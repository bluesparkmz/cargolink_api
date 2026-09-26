import logging
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config import settings
from constants import (
    PAYMENT_METHOD_MPESA,
    PAYMENT_STATUS_COMPLETED,
    PAYMENT_STATUS_FAILED,
    PAYMENT_STATUS_PENDING,
    TRANSACTION_STATUS_COMPLETED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_TYPE_DEPOSIT,
)
from controllers.mpesa_client import mpesa_client, MPESA_POLL_INTERVAL_SECONDS
from controllers.mpesa_utils import (
    build_mpesa_reference,
    classify_mpesa_status,
    extract_transaction_status,
    is_mpesa_c2b_accepted,
    normalize_msisdn,
    normalize_mpesa_reference,
)
from models.models import Payment, Transaction, User, Wallet
from schemas.schemas import WalletDepositRequest

logger = logging.getLogger(__name__)


def get_or_create_wallet(db: Session, user: User) -> Wallet:
    """Garante carteira para o utilizador."""
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet is None:
        wallet = Wallet(user_id=user.id)
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    return wallet


def get_wallet_balance(db: Session, user: User) -> Wallet:
    """Saldo da carteira."""
    return get_or_create_wallet(db, user)


def _prepare_deposit_refs() -> tuple[str, str, str]:
    """
    Gera referências M-Pesa válidas (alfanuméricas, max 20 chars).
    Retorna: (external_reference, transaction_ref, third_party_ref)
    """
    external_reference = build_mpesa_reference("CL")
    transaction_ref = normalize_mpesa_reference(external_reference, fallback_prefix="TX")
    third_party_ref = normalize_mpesa_reference(
        external_reference,
        fallback_prefix="TP",
    )
    return external_reference, transaction_ref, third_party_ref


def _resolve_deposit_phone(user: User, phone: str | None) -> str:
    raw = (phone or user.phone or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Número de telemóvel é obrigatório para depósito M-Pesa.",
        )
    return normalize_msisdn(raw)


def list_wallet_transactions(
    db: Session,
    user: User,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[Transaction]:
    """Extrato de movimentos."""
    wallet = get_or_create_wallet(db, user)
    return (
        db.query(Transaction)
        .filter(Transaction.wallet_id == wallet.id)
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _complete_deposit(
    db: Session,
    wallet: Wallet,
    payment: Payment,
    transaction: Transaction,
) -> None:
    """Credita saldo após confirmação M-Pesa."""
    try:
        amount = payment.amount
        payment.status = PAYMENT_STATUS_COMPLETED
        transaction.status = TRANSACTION_STATUS_COMPLETED
        wallet.available_balance = (wallet.available_balance or Decimal(0)) + amount
        payment.gateway_response = {
            "provider": PAYMENT_METHOD_MPESA,
            "simulated": settings.AUTO_CONFIRM_MPESA_DEPOSITS,
            "status": PAYMENT_STATUS_COMPLETED,
        }
        db.commit()
        logger.info(f"Deposit completed: payment_id={payment.id}, amount={amount}")
    except Exception as e:
        db.rollback()
        logger.error(f"Error completing deposit: {str(e)}", exc_info=True)
        raise


async def create_deposit(db: Session, user: User, data: WalletDepositRequest) -> dict:
    """
    Inicia depósito M-Pesa sem polling bloqueante.
    
    Fluxo:
    1. Cria registros de pagamento e transação como PENDING
    2. Envia requisição ao M-Pesa (assincronamente)
    3. Se aceito (INS-0), retorna imediatamente com status PENDING
    4. Callback do M-Pesa confirma o depósito automaticamente
    """
    wallet = get_or_create_wallet(db, user)
    phone = _resolve_deposit_phone(user, data.phone)
    amount = Decimal(str(data.amount))

    external_reference, transaction_ref, third_party_ref = _prepare_deposit_refs()

    # Cria registro de pagamento (será atualizado com resposta do Mpesa)
    payment = Payment(
        user_id=user.id,
        method=data.method,
        phone=phone,
        amount=amount,
        status=PAYMENT_STATUS_PENDING,
        external_reference=external_reference,
    )
    db.add(payment)
    db.flush()

    # Cria movimento da carteira (pendente até confirmação)
    transaction = Transaction(
        wallet_id=wallet.id,
        transaction_type=TRANSACTION_TYPE_DEPOSIT,
        amount=amount,
        status=TRANSACTION_STATUS_PENDING,
        reference=external_reference,
        description=f"Depósito {data.method.upper()} via {phone}",
    )
    db.add(transaction)
    db.flush()

    # Em desenvolvimento, simula confirmação automática
    if settings.AUTO_CONFIRM_MPESA_DEPOSITS:
        logger.info(f"Auto-confirming M-Pesa deposit for {user.id} (dev mode)")
        _complete_deposit(db, wallet, payment, transaction)
        return {
            "payment_id": payment.id,
            "transaction_id": transaction.id,
            "amount": float(amount),
            "status": "completed",  # Frontend espera inglês
            "external_reference": external_reference,
            "phone": phone,
            "message": "Depósito confirmado (modo desenvolvimento). Saldo atualizado.",
        }

    # ========== PRODUÇÃO: Requisição ao M-Pesa (ASSÍNCRONA) ==========
    try:
        logger.info(f"M-Pesa C2B request: phone={phone}, amount={amount}, ref={transaction_ref}")
        
        mpesa_response = await mpesa_client.initiate_payment_async(
            transaction_reference=transaction_ref,
            customer_msisdn=phone,
            amount=float(amount),
            third_party_reference=third_party_ref,
        )
        
        payment.gateway_response = {"c2b": mpesa_response}
        db.commit()
        
        response_code = mpesa_response.get("output_ResponseCode", "")
        response_desc = mpesa_response.get("output_ResponseDesc", "")
        
        logger.info(f"M-Pesa C2B response: code={response_code}, desc={response_desc}")
        
        if not is_mpesa_c2b_accepted(response_code):
            payment.status = PAYMENT_STATUS_FAILED
            transaction.status = "failed"
            db.commit()
            logger.warning(f"M-Pesa rejected payment {payment.id}: {response_desc}")
            return {
                "payment_id": payment.id,
                "transaction_id": transaction.id,
                "amount": float(amount),
                "status": "failed",
                "external_reference": external_reference,
                "phone": phone,
                "message": f"M-Pesa rejeitou: {response_desc}",
            }

        # Modo teste: M-Pesa aceitou (INS-0 / HTTP 200) → credita sem PIN
        if settings.MPESA_AUTO_CREDIT_ON_ACCEPT:
            logger.info(f"M-Pesa INS-0 auto-credit: payment_id={payment.id}")
            _complete_deposit(db, wallet, payment, transaction)
            return {
                "payment_id": payment.id,
                "transaction_id": transaction.id,
                "amount": float(amount),
                "status": "completed",
                "external_reference": external_reference,
                "phone": phone,
                "message": "Depósito confirmado. Saldo actualizado.",
            }

        logger.info(f"M-Pesa accepted (INS-0). Waiting for PIN: payment_id={payment.id}")
        
        return {
            "payment_id": payment.id,
            "transaction_id": transaction.id,
            "amount": float(amount),
            "status": "pending",
            "external_reference": external_reference,
            "phone": phone,
            "message": (
                "Pedido enviado à Vodacom M-Pesa. "
                "Verifique o diálogo de PIN no seu telemóvel e confirme o pagamento."
            ),
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in create_deposit: {str(e)}", exc_info=True)
        payment.status = PAYMENT_STATUS_FAILED
        transaction.status = "failed"
        payment.gateway_response = {"error": str(e)}
        db.commit()
        
        # Mensagem mais clara sobre o erro
        error_msg = str(e)
        if "403" in error_msg:
            user_msg = "Erro na configuração de autenticação com M-Pesa. Contacte suporte."
        elif "Connection" in error_msg or "timeout" in error_msg.lower():
            user_msg = "Erro de conexão com M-Pesa. Tente novamente."
        else:
            user_msg = f"Erro ao processar depósito: {error_msg[:100]}"
        
        return {
            "payment_id": payment.id,
            "transaction_id": transaction.id,
            "amount": float(amount),
            "status": "failed",  # Frontend espera inglês
            "external_reference": external_reference,
            "phone": phone,
            "message": user_msg,
        }


async def create_deposit_with_polling(
    db: Session, 
    user: User, 
    data: WalletDepositRequest,
    max_wait_seconds: int = 60,
) -> dict:
    """
    Inicia depósito M-Pesa e aguarda confirmação via polling automático.
    
    Diferente de create_deposit() que retorna imediatamente com status PENDING,
    esta função aguarda até 60 segundos para o usuário confirmar no M-Pesa.
    
    Fluxo:
    1. Cria registros de pagamento e transação como PENDING
    2. Envia requisição ao M-Pesa (assincronamente)
    3. Se aceito (INS-0), faz polling automático
    4. Aguarda confirmação ou rejeição do usuário (até 60s)
    5. Retorna resultado final (confirmado/rejeitado/timeout)
    
    Args:
        db: Sessão do banco de dados
        user: Usuário autenticado
        data: Dados do depósito (amount, phone, method)
        max_wait_seconds: Tempo máximo de espera em segundos (padrão: 60)
    
    Returns:
        Dict com status final: confirmado/rejeitado/timeout
    """
    wallet = get_or_create_wallet(db, user)
    phone = _resolve_deposit_phone(user, data.phone)
    amount = Decimal(str(data.amount))

    external_reference, transaction_ref, third_party_ref = _prepare_deposit_refs()

    # Cria registro de pagamento
    payment = Payment(
        user_id=user.id,
        method=data.method,
        phone=phone,
        amount=amount,
        status=PAYMENT_STATUS_PENDING,
        external_reference=external_reference,
    )
    db.add(payment)
    db.flush()

    # Cria movimento da carteira (pendente até confirmação)
    transaction = Transaction(
        wallet_id=wallet.id,
        transaction_type=TRANSACTION_TYPE_DEPOSIT,
        amount=amount,
        status=TRANSACTION_STATUS_PENDING,
        reference=external_reference,
        description=f"Depósito {data.method.upper()} via {phone}",
    )
    db.add(transaction)
    db.flush()

    # Em desenvolvimento, simula confirmação automática
    if settings.AUTO_CONFIRM_MPESA_DEPOSITS:
        logger.info(f"Auto-confirming M-Pesa deposit for {user.id} (dev mode)")
        _complete_deposit(db, wallet, payment, transaction)
        return {
            "payment_id": payment.id,
            "transaction_id": transaction.id,
            "amount": float(amount),
            "status": "completed",
            "external_reference": external_reference,
            "phone": phone,
            "message": "Depósito confirmado (modo desenvolvimento). Saldo atualizado.",
        }

    # ========== PRODUÇÃO: Requisição ao M-Pesa ==========
    try:
        logger.info(f"M-Pesa C2B request (with polling): phone={phone}, amount={amount}, ref={transaction_ref}")
        
        mpesa_response = await mpesa_client.initiate_payment_async(
            transaction_reference=transaction_ref,
            customer_msisdn=phone,
            amount=float(amount),
            third_party_reference=third_party_ref,
        )
        
        payment.gateway_response = {"c2b": mpesa_response}
        db.commit()
        
        response_code = mpesa_response.get("output_ResponseCode", "")
        response_desc = mpesa_response.get("output_ResponseDesc", "")
        
        logger.info(f"M-Pesa C2B response: code={response_code}, desc={response_desc}")
        
        if not is_mpesa_c2b_accepted(response_code):
            payment.status = PAYMENT_STATUS_FAILED
            transaction.status = "failed"
            db.commit()
            logger.warning(f"M-Pesa rejected payment {payment.id}: {response_desc}")
            return {
                "payment_id": payment.id,
                "transaction_id": transaction.id,
                "amount": float(amount),
                "status": "failed",
                "external_reference": external_reference,
                "phone": phone,
                "message": f"M-Pesa rejeitou: {response_desc}",
            }

        if settings.MPESA_AUTO_CREDIT_ON_ACCEPT:
            logger.info(f"M-Pesa INS-0 auto-credit (polling route): payment_id={payment.id}")
            _complete_deposit(db, wallet, payment, transaction)
            return {
                "payment_id": payment.id,
                "transaction_id": transaction.id,
                "amount": float(amount),
                "status": "completed",
                "external_reference": external_reference,
                "phone": phone,
                "message": "Depósito confirmado. Saldo actualizado.",
            }
        
        conversation_id = mpesa_response.get("output_ConversationID", "")
        third_party_ref_sent = mpesa_response.get("_third_party_reference", third_party_ref)
        
        logger.info(
            f"M-Pesa accepted (INS-0). Starting Skywallet polling: "
            f"payment_id={payment.id}, conversation_id={conversation_id}"
        )
        
        max_retries = max(1, max_wait_seconds // MPESA_POLL_INTERVAL_SECONDS)

        polling_result = await mpesa_client.wait_for_payment_confirmation_async(
            c2b_response=mpesa_response,
            transaction_reference=transaction_ref,
            third_party_reference=third_party_ref_sent,
            max_retries=max_retries,
        )
        
        poll_status = polling_result.get("status")

        if poll_status == "success":
            logger.info(f"Payment confirmed after polling: payment_id={payment.id}")
            _complete_deposit(db, wallet, payment, transaction)
            
            return {
                "payment_id": payment.id,
                "transaction_id": transaction.id,
                "amount": float(amount),
                "status": "completed",
                "external_reference": external_reference,
                "phone": phone,
                "message": polling_result.get("message", "Depósito confirmado. Saldo actualizado."),
                "polling_info": {
                    "attempts": polling_result.get("attempts"),
                    "wait_time_seconds": polling_result.get("wait_time_seconds"),
                }
            }
        
        if poll_status == "failed":
            logger.warning(f"Payment rejected after polling: payment_id={payment.id}")
            payment.status = PAYMENT_STATUS_FAILED
            transaction.status = "failed"
            payment.gateway_response = {
                "c2b": mpesa_response,
                "polling": polling_result.get("query_result"),
            }
            db.commit()
            
            return {
                "payment_id": payment.id,
                "transaction_id": transaction.id,
                "amount": float(amount),
                "status": "failed",
                "external_reference": external_reference,
                "phone": phone,
                "message": polling_result.get("message", "Pagamento cancelado ou rejeitado."),
                "polling_info": {
                    "attempts": polling_result.get("attempts"),
                    "wait_time_seconds": polling_result.get("wait_time_seconds"),
                }
            }
        
        # pending / timeout — NÃO creditar
        logger.warning(f"Payment still pending after polling: payment_id={payment.id}")
        db.commit()
        
        return {
            "payment_id": payment.id,
            "transaction_id": transaction.id,
            "amount": float(amount),
            "status": "pending",
            "external_reference": external_reference,
            "phone": phone,
            "message": polling_result.get(
                "message",
                "Pagamento pendente. Confirme no telemóvel ou tente novamente.",
            ),
            "polling_info": {
                "attempts": polling_result.get("attempts"),
                "wait_time_seconds": polling_result.get("wait_time_seconds"),
            }
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in create_deposit_with_polling: {str(e)}", exc_info=True)
        payment.status = PAYMENT_STATUS_FAILED
        transaction.status = "failed"
        payment.gateway_response = {"error": str(e)}
        db.commit()
        
        # Mensagem mais clara sobre o erro
        error_msg = str(e)
        if "403" in error_msg:
            user_msg = "Erro na configuração de autenticação com M-Pesa. Contacte suporte."
        elif "Connection" in error_msg or "timeout" in error_msg.lower():
            user_msg = "Erro de conexão com M-Pesa. Tente novamente."
        else:
            user_msg = f"Erro ao processar depósito: {error_msg[:100]}"
        
        return {
            "payment_id": payment.id,
            "transaction_id": transaction.id,
            "amount": float(amount),
            "status": "failed",
            "external_reference": external_reference,
            "phone": phone,
            "message": user_msg,
        }


def get_deposit_status(db: Session, user: User, payment_id: int) -> dict:
    """Estado actual do depósito (para polling do frontend)."""
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pagamento não encontrado")
    if payment.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso")
    if payment.method != PAYMENT_METHOD_MPESA or payment.load_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pagamento inválido para depósito na carteira",
        )

    status_map = {
        PAYMENT_STATUS_COMPLETED: "completed",
        PAYMENT_STATUS_PENDING: "pending",
        PAYMENT_STATUS_FAILED: "failed",
    }
    mapped_status = status_map.get(payment.status, payment.status)

    messages = {
        "completed": "Depósito confirmado.",
        "pending": "Aguardando confirmação no telemóvel.",
        "failed": "Depósito cancelado ou rejeitado.",
    }

    return {
        "payment_id": payment.id,
        "amount": float(payment.amount),
        "status": mapped_status,
        "external_reference": payment.external_reference or "",
        "phone": payment.phone or user.phone,
        "message": messages.get(mapped_status, "Estado do depósito actualizado."),
    }


def sync_deposit_with_mpesa(db: Session, user: User, payment_id: int) -> dict:
    """
    Consulta M-Pesa (verificador) e actualiza o depósito pendente.
    Chamado pelo frontend a cada poucos segundos (máx. ~60s).
    """
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pagamento não encontrado")
    if payment.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso")
    if payment.method != PAYMENT_METHOD_MPESA or payment.load_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pagamento inválido para depósito na carteira",
        )

    status_map = {
        PAYMENT_STATUS_COMPLETED: "completed",
        PAYMENT_STATUS_PENDING: "pending",
        PAYMENT_STATUS_FAILED: "failed",
    }

    if payment.status in (PAYMENT_STATUS_COMPLETED, PAYMENT_STATUS_FAILED):
        mapped = status_map.get(payment.status, payment.status)
        return {
            "payment_id": payment.id,
            "amount": float(payment.amount),
            "status": mapped,
            "external_reference": payment.external_reference or "",
            "phone": payment.phone or user.phone,
            "message": "Depósito confirmado." if mapped == "completed" else "Depósito cancelado ou rejeitado.",
        }

    gateway = payment.gateway_response if isinstance(payment.gateway_response, dict) else {}
    c2b_response = gateway.get("c2b") if isinstance(gateway.get("c2b"), dict) else gateway
    tx_ref = c2b_response.get("_transaction_reference") or payment.external_reference or ""
    tp_ref = c2b_response.get("_third_party_reference") or payment.external_reference or ""

    query_result = mpesa_client.query_status_with_candidates(c2b_response, tx_ref, tp_ref)

    wallet = get_or_create_wallet(db, user)
    transaction = (
        db.query(Transaction)
        .filter(
            Transaction.wallet_id == wallet.id,
            Transaction.reference == payment.external_reference,
        )
        .first()
    )

    if not query_result.get("success"):
        return {
            "payment_id": payment.id,
            "amount": float(payment.amount),
            "status": "pending",
            "external_reference": payment.external_reference or "",
            "phone": payment.phone or user.phone,
            "message": "A aguardar confirmação no telemóvel...",
        }

    final_state = query_result.get("status")
    payment.gateway_response = {
        "c2b": c2b_response,
        "last_query": query_result,
    }

    if final_state == "success" and transaction:
        _complete_deposit(db, wallet, payment, transaction)
        return {
            "payment_id": payment.id,
            "amount": float(payment.amount),
            "status": "completed",
            "external_reference": payment.external_reference or "",
            "phone": payment.phone or user.phone,
            "message": "Depósito confirmado. Saldo actualizado.",
        }

    if final_state == "failed":
        payment.status = PAYMENT_STATUS_FAILED
        if transaction:
            transaction.status = "failed"
        db.commit()
        return {
            "payment_id": payment.id,
            "amount": float(payment.amount),
            "status": "failed",
            "external_reference": payment.external_reference or "",
            "phone": payment.phone or user.phone,
            "message": query_result.get("response_description") or "Pagamento cancelado ou rejeitado.",
        }

    db.commit()
    return {
        "payment_id": payment.id,
        "amount": float(payment.amount),
        "status": "pending",
        "external_reference": payment.external_reference or "",
        "phone": payment.phone or user.phone,
        "message": "Confirme o pagamento no seu telemóvel.",
    }


def confirm_deposit(db: Session, user: User, payment_id: int) -> dict:
    """
    Confere o estado real do depósito no M-Pesa.

    Este endpoint é mantido por compatibilidade com versões antigas do app,
    mas nunca pode creditar saldo apenas porque o utilizador tocou em
    "confirmar". O crédito só acontece após confirmação do gateway.
    """
    return sync_deposit_with_mpesa(db, user, payment_id)


def process_mpesa_callback(payload: dict) -> dict:
    """
    Processa callback do M-Pesa após pagamento confirmado.
    
    Este é o ponto onde M-Pesa notifica que o utilizador confirmou o pagamento.
    
    Esperado no payload:
    - output_ConversationID: ID da conversa
    - output_ResponseCode: Código de resposta (INS0 = sucesso)
    - output_ResponseDesc: Descrição da resposta
    - input_ThirdPartyReference: Nossa referência externa (external_reference)
    
    Fluxo:
    1. Localiza o pagamento pela external_reference
    2. Se sucesso (INS0), credita o saldo
    3. Se falha, marca como failed
    """
    from database import SessionLocal
    
    db = SessionLocal()
    try:
        # Extrai info do callback
        conversation_id = payload.get("output_ConversationID", "")
        response_code = payload.get("output_ResponseCode", "").upper()
        response_desc = payload.get("output_ResponseDesc", "")
        third_party_ref = payload.get("input_ThirdPartyReference", "")
        
        logger.info(
            f"M-Pesa callback: conversation={conversation_id}, "
            f"code={response_code}, ref={third_party_ref}"
        )
        
        if not third_party_ref:
            logger.error("M-Pesa callback missing input_ThirdPartyReference")
            return {
                "status": "error",
                "message": "Referência do M-Pesa ausente no callback",
            }
        
        # Localiza o pagamento pela nossa external_reference
        payment = (
            db.query(Payment)
            .filter(Payment.external_reference == third_party_ref)
            .first()
        )
        
        if payment is None:
            logger.warning(f"Payment not found for reference: {third_party_ref}")
            return {
                "status": "error",
                "message": "Pagamento não encontrado com referência fornecida",
            }
        
        # Se pagamento já foi processado, ignora callback duplicado
        if payment.status in (PAYMENT_STATUS_COMPLETED, PAYMENT_STATUS_FAILED):
            logger.info(
                f"Payment {payment.id} already processed (status: {payment.status}). "
                "Ignoring duplicate callback."
            )
            return {
                "status": "ignored",
                "message": f"Pagamento já foi processado com status: {payment.status}",
            }
        
        transaction_status = extract_transaction_status(payload)
        final_state = classify_mpesa_status(
            payload.get("output_ResponseCode"),
            transaction_status,
            payload.get("output_ResponseDesc"),
        )

        logger.info(
            f"M-Pesa callback classified: payment_id={payment.id if payment else '?'} "
            f"state={final_state} tx_status={transaction_status}"
        )

        if final_state == "success":
            user = db.query(User).filter(User.id == payment.user_id).first()
            if not user:
                logger.error(f"User not found for payment {payment.id}")
                return {"status": "error", "message": "Utilizador não encontrado"}

            wallet = get_or_create_wallet(db, user)
            
            # Localiza a transação pendente
            transaction = (
                db.query(Transaction)
                .filter(
                    Transaction.wallet_id == wallet.id,
                    Transaction.reference == payment.external_reference,
                )
                .first()
            )
            
            if not transaction:
                logger.error(f"Transaction not found for payment {payment.id}")
                payment.status = PAYMENT_STATUS_FAILED
                payment.gateway_response = payload
                db.commit()
                return {
                    "status": "error",
                    "message": "Movimento não encontrado",
                }
            
            # Credita o saldo
            _complete_deposit(db, wallet, payment, transaction)
            payment_id_val = payment.id
            
            logger.info(f"✅ Deposit confirmed via M-Pesa callback: payment_id={payment_id_val}, amount={payment.amount}")
            return {
                "status": "success",
                "message": "Depósito confirmado",
                "payment_id": payment_id_val,
            }
        
        elif final_state == "failed":
            payment.status = PAYMENT_STATUS_FAILED
            payment.gateway_response = payload
            
            transaction = (
                db.query(Transaction)
                .filter(Transaction.reference == payment.external_reference)
                .first()
            )
            if transaction:
                transaction.status = "failed"
            
            db.commit()
            logger.warning(
                f"M-Pesa payment rejected via callback: payment_id={payment.id}, "
                f"code={response_code}, desc={response_desc}"
            )
            return {
                "status": "failed",
                "message": f"Pagamento rejeitado: {response_desc}",
                "payment_id": payment.id,
            }

        else:
            payment.gateway_response = payload
            db.commit()
            logger.info(
                f"M-Pesa callback pending for payment {payment.id}: "
                f"code={response_code}, status={transaction_status}"
            )
            return {
                "status": "pending",
                "message": "Callback recebido; pagamento ainda pendente.",
                "payment_id": payment.id,
            }
            
    except Exception as e:
        logger.error(f"Error processing M-Pesa callback: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"Erro ao processar callback: {str(e)}",
        }
    finally:
        db.close()

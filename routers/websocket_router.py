"""
WebSocket realtime: viagens, GPS e presenca dos perfis.

Conexao:
ws://host/ws?token=<jwt>

Eventos recebidos:
- subscribe_trip: {"type":"subscribe_trip","trip_id":1}
- unsubscribe_trip: {"type":"unsubscribe_trip","trip_id":1}
- driver_location: {"type":"driver_location","trip_id":1,"latitude":-25.9,"longitude":32.5}
- message_send: {"type":"message_send","load_id":1,"receiver_id":2,"body":"Ola"}
- message_read: {"type":"message_read","message_id":1}
- subscribe_proposal: {"type":"subscribe_proposal","proposal_id":1}
- negotiation_create: {"type":"negotiation_create","proposal_id":1,"amount":25000}
- negotiation_accept: {"type":"negotiation_accept","proposal_id":1,"negotiation_id":2}
- negotiation_reject: {"type":"negotiation_reject","proposal_id":1,"negotiation_id":2}
"""

from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from constants import TRIP_GROUP_STATUSES, TRIP_GROUP_IN_PROGRESS
from controllers.connection_manager import connection_manager
from controllers.driver_trips_controller import add_driver_location
from controllers.messages_controller import mark_message_read, send_message
from controllers.proposals_controller import (
    accept_counter_offer,
    accept_proposal_by_id,
    create_counter_offer,
    get_proposal_detail,
    reject_counter_offer,
    reject_proposal_by_id,
)
from database import SessionLocal
from models.models import Client, Company, Driver, Load, LoadProposal, Trip, User
from schemas.schemas import (
    MessageCreateRequest,
    ProposalNegotiationCreateRequest,
    TripLocationCreateRequest,
)
from security import decode_token

router = APIRouter()


def _authenticate(token: str | None, db: Session) -> User | None:
    if not token:
        return None
    payload = decode_token(token)
    if payload is None or payload.get("sub") is None:
        return None
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if user is None or user.status != "ativo":
        return None
    return user


def _profile_rooms(db: Session, user: User) -> set[str]:
    rooms = {f"user:{user.id}", f"role:{user.user_type}"}

    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        if client:
            rooms.add(f"client:{client.id}")

    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if company:
            rooms.add(f"company:{company.id}")

    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if driver:
            rooms.add(f"driver:{driver.id}")
            if driver.company_id:
                rooms.add(f"company:{driver.company_id}")

    return rooms


def _active_trips_for_user(db: Session, user: User) -> list[Trip]:
    active_statuses = TRIP_GROUP_STATUSES[TRIP_GROUP_IN_PROGRESS]

    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if not driver:
            return []
        return db.query(Trip).filter(Trip.driver_id == driver.id, Trip.status.in_(active_statuses)).all()

    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if not company:
            return []
        return db.query(Trip).filter(Trip.company_id == company.id, Trip.status.in_(active_statuses)).all()

    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        if not client:
            return []
        return (
            db.query(Trip)
            .join(Load, Load.id == Trip.load_id)
            .filter(Load.client_id == client.id, Trip.status.in_(active_statuses))
            .all()
        )

    return db.query(Trip).filter(Trip.status.in_(active_statuses)).all()


def _can_access_trip(db: Session, user: User, trip: Trip) -> bool:
    if user.user_type == "admin":
        return True

    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        return driver is not None and trip.driver_id == driver.id

    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        return company is not None and trip.company_id == company.id

    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        load = db.query(Load).filter(Load.id == trip.load_id).first()
        return client is not None and load is not None and load.client_id == client.id

    return False


def _can_access_load(db: Session, user: User, load_id: int) -> bool:
    load = db.query(Load).filter(Load.id == load_id).first()
    if load is None:
        return False
    if user.user_type == "admin":
        return True

    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        return client is not None and load.client_id == client.id

    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if not company:
            return False
        has_trip = db.query(Trip).filter(Trip.load_id == load_id, Trip.company_id == company.id).first()
        has_proposal = (
            db.query(LoadProposal)
            .filter(LoadProposal.load_id == load_id, LoadProposal.company_id == company.id)
            .first()
        )
        return has_trip is not None or has_proposal is not None

    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if not driver:
            return False
        has_trip = db.query(Trip).filter(Trip.load_id == load_id, Trip.driver_id == driver.id).first()
        has_proposal = (
            db.query(LoadProposal)
            .filter(LoadProposal.load_id == load_id, LoadProposal.driver_id == driver.id)
            .first()
        )
        return has_trip is not None or has_proposal is not None

    return False


async def _join_trip_rooms(websocket: WebSocket, trip: Trip) -> None:
    await connection_manager.join(websocket, f"trip:{trip.id}")
    await connection_manager.join(websocket, f"load:{trip.load_id}")
    if trip.company_id:
        await connection_manager.join(websocket, f"company:{trip.company_id}")
    if trip.driver_id:
        await connection_manager.join(websocket, f"driver:{trip.driver_id}")


async def _join_proposal_rooms(websocket: WebSocket, proposal: LoadProposal) -> None:
    await connection_manager.join(websocket, f"proposal:{proposal.id}")
    await connection_manager.join(websocket, f"load:{proposal.load_id}")
    if proposal.company_id:
        await connection_manager.join(websocket, f"company:{proposal.company_id}")
    if proposal.driver_id:
        await connection_manager.join(websocket, f"driver:{proposal.driver_id}")


async def _join_initial_rooms(websocket: WebSocket, db: Session, user: User) -> None:
    for room in _profile_rooms(db, user):
        await connection_manager.join(websocket, room)
    for trip in _active_trips_for_user(db, user):
        await _join_trip_rooms(websocket, trip)


async def _send_error(websocket: WebSocket, message: str, code: str = "bad_request") -> None:
    await connection_manager.send_personal(
        websocket,
        {"type": "error", "code": code, "message": message},
    )


def _event_rooms_for_trip(trip: Trip) -> set[str]:
    rooms = {f"trip:{trip.id}", f"load:{trip.load_id}"}
    if trip.company_id:
        rooms.add(f"company:{trip.company_id}")
    if trip.driver_id:
        rooms.add(f"driver:{trip.driver_id}")
    return rooms


async def _handle_subscribe_trip(websocket: WebSocket, db: Session, user: User, data: dict[str, Any]) -> None:
    trip_id = data.get("trip_id")
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if trip is None or not _can_access_trip(db, user, trip):
        await _send_error(websocket, "Sem acesso a esta viagem", "forbidden")
        return
    await _join_trip_rooms(websocket, trip)
    await connection_manager.send_personal(
        websocket,
        {"type": "subscription_ok", "scope": "trip", "trip_id": trip.id, "load_id": trip.load_id},
    )


async def _handle_unsubscribe_trip(websocket: WebSocket, data: dict[str, Any]) -> None:
    trip_id = data.get("trip_id")
    if not isinstance(trip_id, int):
        await _send_error(websocket, "trip_id invalido")
        return
    await connection_manager.leave(websocket, f"trip:{trip_id}")
    await connection_manager.send_personal(
        websocket,
        {"type": "subscription_closed", "scope": "trip", "trip_id": trip_id},
    )


async def _handle_subscribe_load(websocket: WebSocket, db: Session, user: User, data: dict[str, Any]) -> None:
    load_id = data.get("load_id")
    if not isinstance(load_id, int) or not _can_access_load(db, user, load_id):
        await _send_error(websocket, "Sem acesso a esta carga", "forbidden")
        return
    await connection_manager.join(websocket, f"load:{load_id}")
    await connection_manager.send_personal(
        websocket,
        {"type": "subscription_ok", "scope": "load", "load_id": load_id},
    )


async def _handle_subscribe_proposal(
    websocket: WebSocket, db: Session, user: User, data: dict[str, Any]
) -> None:
    proposal_id = data.get("proposal_id")
    if not isinstance(proposal_id, int):
        await _send_error(websocket, "proposal_id invalido")
        return
    try:
        proposal = get_proposal_detail(db, user, proposal_id)
    except HTTPException as exc:
        await _send_error(websocket, str(exc.detail), "request_error")
        return
    await _join_proposal_rooms(websocket, proposal)
    await connection_manager.send_personal(
        websocket,
        {
            "type": "subscription_ok",
            "scope": "proposal",
            "proposal_id": proposal.id,
            "load_id": proposal.load_id,
        },
    )


async def _handle_driver_location(websocket: WebSocket, db: Session, user: User, data: dict[str, Any]) -> None:
    if user.user_type != "motorista":
        await _send_error(websocket, "Apenas motorista envia GPS da viagem", "forbidden")
        return

    trip_id = data.get("trip_id")
    if not isinstance(trip_id, int):
        await _send_error(websocket, "trip_id invalido")
        return

    location_data = data.get("location")
    if not isinstance(location_data, dict):
        location_data = data

    try:
        payload = TripLocationCreateRequest(
            latitude=location_data.get("latitude"),
            longitude=location_data.get("longitude"),
            speed=location_data.get("speed"),
            traveled_distance_km=location_data.get("traveled_distance_km"),
        )
    except ValidationError as exc:
        await _send_error(websocket, str(exc), "validation_error")
        return

    try:
        stored_location = add_driver_location(db, user, trip_id, payload)
    except HTTPException as exc:
        db.rollback()
        await _send_error(websocket, str(exc.detail), "request_error")
        return

    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if trip is None:
        await _send_error(websocket, "Viagem nao encontrada", "not_found")
        return

    event = {
        "type": "trip.location",
        "trip_id": trip.id,
        "load_id": trip.load_id,
        "company_id": trip.company_id,
        "driver_id": trip.driver_id,
        "vehicle_id": trip.vehicle_id,
        "location": {
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "speed": payload.speed,
            "traveled_distance_km": payload.traveled_distance_km,
            "stored_location_id": stored_location.id if stored_location else None,
            "stored_location_created_at": stored_location.created_at if stored_location else None,
            "phase": trip.status,
        },
    }
    await connection_manager.broadcast_rooms(_event_rooms_for_trip(trip), event)


async def _handle_message_send(websocket: WebSocket, db: Session, user: User, data: dict[str, Any]) -> None:
    load_id = data.get("load_id")
    if not isinstance(load_id, int):
        await _send_error(websocket, "load_id invalido")
        return

    try:
        payload = MessageCreateRequest(
            receiver_id=data.get("receiver_id"),
            body=data.get("body"),
            attachment=data.get("attachment"),
        )
    except ValidationError as exc:
        await _send_error(websocket, str(exc), "validation_error")
        return

    try:
        message = send_message(db, user, load_id, payload)
    except HTTPException as exc:
        db.rollback()
        await _send_error(websocket, str(exc.detail), "request_error")
        return

    await connection_manager.send_personal(
        websocket,
        {
            "type": "message.sent",
            "load_id": load_id,
            "message_id": message.id,
        },
    )


async def _handle_message_read(websocket: WebSocket, db: Session, user: User, data: dict[str, Any]) -> None:
    message_id = data.get("message_id")
    if not isinstance(message_id, int):
        await _send_error(websocket, "message_id invalido")
        return
    try:
        message = mark_message_read(db, user, message_id)
    except HTTPException as exc:
        db.rollback()
        await _send_error(websocket, str(exc.detail), "request_error")
        return
    event = {
        "type": "message.read",
        "message_id": message.id,
        "load_id": message.load_id,
        "reader_id": user.id,
    }
    await connection_manager.broadcast_rooms(
        {f"user:{message.sender_id}", f"user:{message.receiver_id}", f"load:{message.load_id}"} - {"user:None"},
        event,
    )


async def _handle_negotiation_create(
    websocket: WebSocket, db: Session, user: User, data: dict[str, Any]
) -> None:
    proposal_id = data.get("proposal_id")
    if not isinstance(proposal_id, int):
        await _send_error(websocket, "proposal_id invalido")
        return
    try:
        payload = ProposalNegotiationCreateRequest(
            amount=data.get("amount"),
            message=data.get("message"),
        )
    except ValidationError as exc:
        await _send_error(websocket, str(exc), "validation_error")
        return
    try:
        negotiation = create_counter_offer(db, user, proposal_id, payload)
    except HTTPException as exc:
        db.rollback()
        await _send_error(websocket, str(exc.detail), "request_error")
        return
    await connection_manager.send_personal(
        websocket,
        {
            "type": "negotiation.sent",
            "proposal_id": proposal_id,
            "negotiation_id": negotiation.id,
        },
    )


async def _handle_negotiation_decision(
    websocket: WebSocket,
    db: Session,
    user: User,
    data: dict[str, Any],
    *,
    decision: str,
) -> None:
    proposal_id = data.get("proposal_id")
    negotiation_id = data.get("negotiation_id")
    if not isinstance(proposal_id, int) or not isinstance(negotiation_id, int):
        await _send_error(websocket, "proposal_id ou negotiation_id invalido")
        return
    try:
        if decision == "accept":
            proposal = accept_counter_offer(db, user, proposal_id, negotiation_id)
            event_type = "negotiation.accepted_ack"
        else:
            proposal = reject_counter_offer(db, user, proposal_id, negotiation_id)
            event_type = "negotiation.rejected_ack"
    except HTTPException as exc:
        db.rollback()
        await _send_error(websocket, str(exc.detail), "request_error")
        return
    await connection_manager.send_personal(
        websocket,
        {
            "type": event_type,
            "proposal_id": proposal.id,
            "negotiation_id": negotiation_id,
            "status": proposal.status,
        },
    )


async def _handle_proposal_decision(
    websocket: WebSocket,
    db: Session,
    user: User,
    data: dict[str, Any],
    *,
    decision: str,
) -> None:
    proposal_id = data.get("proposal_id")
    if not isinstance(proposal_id, int):
        await _send_error(websocket, "proposal_id invalido")
        return
    try:
        if decision == "accept":
            proposal = accept_proposal_by_id(db, user, proposal_id)
            event_type = "proposal.accepted_ack"
        else:
            proposal = reject_proposal_by_id(db, user, proposal_id)
            event_type = "proposal.rejected_ack"
    except HTTPException as exc:
        db.rollback()
        await _send_error(websocket, str(exc.detail), "request_error")
        return
    await connection_manager.send_personal(
        websocket,
        {
            "type": event_type,
            "proposal_id": proposal.id,
            "load_id": proposal.load_id,
            "status": proposal.status,
        },
    )


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    db = SessionLocal()
    user = _authenticate(token, db)
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        db.close()
        return

    await connection_manager.connect(websocket, user.id, user.user_type)
    await _join_initial_rooms(websocket, db, user)
    await connection_manager.send_personal(
        websocket,
        {
            "type": "websocket.connected",
            "user": {"id": user.id, "type": user.user_type, "name": user.name},
            "active_connections": connection_manager.online_count(),
        },
    )

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            if event_type == "ping":
                await connection_manager.send_personal(websocket, {"type": "pong"})
            elif event_type == "subscribe_trip":
                await _handle_subscribe_trip(websocket, db, user, data)
            elif event_type == "unsubscribe_trip":
                await _handle_unsubscribe_trip(websocket, data)
            elif event_type == "subscribe_load":
                await _handle_subscribe_load(websocket, db, user, data)
            elif event_type == "subscribe_proposal":
                await _handle_subscribe_proposal(websocket, db, user, data)
            elif event_type == "driver_location":
                await _handle_driver_location(websocket, db, user, data)
            elif event_type == "message_send":
                await _handle_message_send(websocket, db, user, data)
            elif event_type == "message_read":
                await _handle_message_read(websocket, db, user, data)
            elif event_type == "negotiation_create":
                await _handle_negotiation_create(websocket, db, user, data)
            elif event_type == "negotiation_accept":
                await _handle_negotiation_decision(websocket, db, user, data, decision="accept")
            elif event_type == "negotiation_reject":
                await _handle_negotiation_decision(websocket, db, user, data, decision="reject")
            elif event_type == "proposal_accept":
                await _handle_proposal_decision(websocket, db, user, data, decision="accept")
            elif event_type == "proposal_reject":
                await _handle_proposal_decision(websocket, db, user, data, decision="reject")
            else:
                await _send_error(websocket, "Tipo de evento desconhecido", "unknown_event")
    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)
    finally:
        connection_manager.disconnect(websocket)
        db.close()

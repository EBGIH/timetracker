"""Organisation and policy configuration (Module A, plus the policy objects of
Modules E, F and G). NFR-M-01: everything here is changeable through the UI
without a code release."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (
    AbsencePolicy,
    BreakType,
    CostCentre,
    Holiday,
    Location,
    Organisation,
    Team,
    User,
    UserGroup,
    new_id,
)
from ..schemas import (
    AbsencePolicyIn,
    BreakTypeIn,
    CostCentreIn,
    GroupIn,
    HolidayImport,
    HolidayIn,
    LocationIn,
    OrganisationUpdate,
    OvertimeRuleIn,
    RuleParamsIn,
    TeamIn,
)
from ..security import Principal, get_principal
from ..services import calc, holidays as holiday_source, rules

router = APIRouter(prefix="/api/org", tags=["organisation"])


def current_org(principal: Principal, db: Session) -> Organisation:
    org = db.get(Organisation, principal.org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organisation not found")
    return org


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("")
def read_org(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    org = current_org(principal, db)
    return {
        c.key: getattr(org, c.key)
        for c in org.__mapper__.column_attrs
    }


@router.put("")
def update_org(
    payload: OrganisationUpdate,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    principal.require("configure_org")
    org = current_org(principal, db)
    before = audit.snapshot(org)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(org, field, value)
    audit.record_for(
        db, principal, request, action="org.updated", entity_type="organisation",
        entity_id=org.id, before=before, after=org,
    )
    db.commit()
    return {"status": "ok"}


@router.get("/rule-params")
def read_rule_params(
    on: date | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    org = current_org(principal, db)
    return {
        "params": rules.rule_params(db, org, on or date.today()),
        "defaults": rules.DEFAULT_PARAMS,
    }


@router.put("/rule-params")
def update_rule_params(
    payload: RuleParamsIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Section 16: parameters are versioned, so historic evaluations keep the
    parameters that were in force at the time."""
    principal.require("configure_policies")
    org = current_org(principal, db)
    before = rules.rule_params(db, org, payload.effective_from)
    version = rules.save_rule_params(
        db, org, payload.params, payload.effective_from, principal.id
    )
    audit.record_for(
        db, principal, request, action="rule_params.updated",
        entity_type="rule_param_version", entity_id=version.id,
        before=before, after=version.params,
    )
    db.commit()
    return {"status": "ok", "effective_from": payload.effective_from}


# ---------------------------------------------------------------------------
# Locations, teams, cost centres
# ---------------------------------------------------------------------------


@router.get("/locations")
def list_locations(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(Location).where(Location.org_id == principal.org_id)).all()
    return [
        {
            "id": r.id, "name": r.name, "address": r.address, "timezone": r.timezone,
            "geo_lat": r.geo_lat, "geo_lng": r.geo_lng, "geo_radius_m": r.geo_radius_m,
        }
        for r in rows
    ]


@router.post("/locations", status_code=201)
def create_location(
    payload: LocationIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    location = Location(id=new_id(), org_id=principal.org_id, **payload.model_dump())
    db.add(location)
    audit.record_for(
        db, principal, request, action="location.created", entity_type="location",
        entity_id=location.id, after=location,
    )
    db.commit()
    return {"id": location.id}


@router.put("/locations/{location_id}")
def update_location(
    location_id: str, payload: LocationIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    location = db.get(Location, location_id)
    if location is None or location.org_id != principal.org_id:
        raise HTTPException(404, "Location not found")
    before = audit.snapshot(location)
    for field, value in payload.model_dump().items():
        setattr(location, field, value)
    audit.record_for(
        db, principal, request, action="location.updated", entity_type="location",
        entity_id=location.id, before=before, after=location,
    )
    db.commit()
    return {"status": "ok"}


@router.get("/teams")
def list_teams(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(Team).where(Team.org_id == principal.org_id)).all()
    counts = {}
    for user in db.scalars(select(User).where(User.org_id == principal.org_id)).all():
        counts[user.team_id] = counts.get(user.team_id, 0) + 1
    return [
        {
            "id": r.id, "name": r.name, "parent_team_id": r.parent_team_id,
            "manager_user_id": r.manager_user_id, "member_count": counts.get(r.id, 0),
        }
        for r in rows
    ]


@router.post("/teams", status_code=201)
def create_team(
    payload: TeamIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    team = Team(id=new_id(), org_id=principal.org_id, **payload.model_dump())
    db.add(team)
    audit.record_for(
        db, principal, request, action="team.created", entity_type="team",
        entity_id=team.id, after=team,
    )
    db.commit()
    return {"id": team.id}


@router.put("/teams/{team_id}")
def update_team(
    team_id: str, payload: TeamIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    team = db.get(Team, team_id)
    if team is None or team.org_id != principal.org_id:
        raise HTTPException(404, "Team not found")
    if payload.parent_team_id:
        # Guard against cycles in the hierarchy.
        cursor, seen = payload.parent_team_id, {team_id}
        while cursor:
            if cursor in seen:
                raise HTTPException(400, "That parent would create a cycle")
            seen.add(cursor)
            parent = db.get(Team, cursor)
            cursor = parent.parent_team_id if parent else None
    before = audit.snapshot(team)
    for field, value in payload.model_dump().items():
        setattr(team, field, value)
    audit.record_for(
        db, principal, request, action="team.updated", entity_type="team",
        entity_id=team.id, before=before, after=team,
    )
    db.commit()
    return {"status": "ok"}


@router.get("/cost-centres")
def list_cost_centres(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(CostCentre).where(
            CostCentre.org_id == principal.org_id, CostCentre.archived.is_(False)
        )
    ).all()
    return [{"id": r.id, "code": r.code, "name": r.name} for r in rows]


@router.post("/cost-centres", status_code=201)
def create_cost_centre(
    payload: CostCentreIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    centre = CostCentre(id=new_id(), org_id=principal.org_id, **payload.model_dump())
    db.add(centre)
    audit.record_for(
        db, principal, request, action="cost_centre.created", entity_type="cost_centre",
        entity_id=centre.id, after=centre,
    )
    db.commit()
    return {"id": centre.id}


# ---------------------------------------------------------------------------
# Holidays (FR-A-06)
# ---------------------------------------------------------------------------


@router.get("/holidays")
def list_holidays(
    year: int | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    query = select(Holiday).where(Holiday.org_id == principal.org_id)
    if year:
        query = query.where(Holiday.day >= date(year, 1, 1), Holiday.day <= date(year, 12, 31))
    rows = db.scalars(query.order_by(Holiday.day)).all()
    return [
        {
            "id": r.id, "day": r.day, "name": r.name, "location_id": r.location_id,
            "is_working_day_override": r.is_working_day_override,
        }
        for r in rows
    ]


@router.post("/holidays", status_code=201)
def create_holiday(
    payload: HolidayIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    holiday = Holiday(id=new_id(), org_id=principal.org_id, **payload.model_dump())
    db.add(holiday)
    audit.record_for(
        db, principal, request, action="holiday.created", entity_type="holiday",
        entity_id=holiday.id, after=holiday,
    )
    db.commit()
    return {"id": holiday.id}


@router.delete("/holidays/{holiday_id}")
def delete_holiday(
    holiday_id: str, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    holiday = db.get(Holiday, holiday_id)
    if holiday is None or holiday.org_id != principal.org_id:
        raise HTTPException(404, "Holiday not found")
    audit.record_for(
        db, principal, request, action="holiday.deleted", entity_type="holiday",
        entity_id=holiday.id, before=holiday,
    )
    db.delete(holiday)
    db.commit()
    return {"status": "deleted"}


@router.post("/holidays/import")
def import_holidays(
    payload: HolidayImport, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    created = 0
    for day, name in holiday_source.for_year(payload.country, payload.year):
        exists = db.scalar(
            select(Holiday).where(
                Holiday.org_id == principal.org_id,
                Holiday.day == day,
                Holiday.location_id == payload.location_id,
            )
        )
        if exists:
            continue
        db.add(
            Holiday(
                id=new_id(), org_id=principal.org_id, location_id=payload.location_id,
                day=day, name=name,
            )
        )
        created += 1
    audit.record_for(
        db, principal, request, action="holiday.imported", entity_type="holiday",
        after={"year": payload.year, "country": payload.country, "created": created},
    )
    db.commit()
    return {"created": created, "year": payload.year}


# ---------------------------------------------------------------------------
# Break types (Module E)
# ---------------------------------------------------------------------------


@router.get("/break-types")
def list_break_types(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(BreakType).where(BreakType.org_id == principal.org_id)).all()
    return [
        {"id": r.id, "name": r.name, "is_paid": r.is_paid, "max_minutes": r.max_minutes}
        for r in rows
    ]


@router.post("/break-types", status_code=201)
def create_break_type(
    payload: BreakTypeIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    break_type = BreakType(id=new_id(), org_id=principal.org_id, **payload.model_dump())
    db.add(break_type)
    audit.record_for(
        db, principal, request, action="break_type.created", entity_type="break_type",
        entity_id=break_type.id, after=break_type,
    )
    db.commit()
    return {"id": break_type.id}


@router.put("/break-types/{break_type_id}")
def update_break_type(
    break_type_id: str, payload: BreakTypeIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    break_type = db.get(BreakType, break_type_id)
    if break_type is None or break_type.org_id != principal.org_id:
        raise HTTPException(404, "Break type not found")
    before = audit.snapshot(break_type)
    for field, value in payload.model_dump().items():
        setattr(break_type, field, value)
    audit.record_for(
        db, principal, request, action="break_type.updated", entity_type="break_type",
        entity_id=break_type.id, before=before, after=break_type,
    )
    db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Absence policies (Module F)
# ---------------------------------------------------------------------------


@router.get("/absence-policies")
def list_policies(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(AbsencePolicy).where(
            AbsencePolicy.org_id == principal.org_id, AbsencePolicy.archived.is_(False)
        )
    ).all()
    return [
        {c.key: getattr(r, c.key) for c in r.__mapper__.column_attrs} for r in rows
    ]


@router.post("/absence-policies", status_code=201)
def create_policy(
    payload: AbsencePolicyIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    policy = AbsencePolicy(id=new_id(), org_id=principal.org_id, **payload.model_dump())
    db.add(policy)
    audit.record_for(
        db, principal, request, action="absence_policy.created",
        entity_type="absence_policy", entity_id=policy.id, after=policy,
    )
    db.commit()
    return {"id": policy.id}


@router.put("/absence-policies/{policy_id}")
def update_policy(
    policy_id: str, payload: AbsencePolicyIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    policy = db.get(AbsencePolicy, policy_id)
    if policy is None or policy.org_id != principal.org_id:
        raise HTTPException(404, "Policy not found")
    before = audit.snapshot(policy)
    for field, value in payload.model_dump().items():
        setattr(policy, field, value)
    audit.record_for(
        db, principal, request, action="absence_policy.updated",
        entity_type="absence_policy", entity_id=policy.id, before=before, after=policy,
    )
    db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Overtime rule (Module G)
# ---------------------------------------------------------------------------


@router.get("/overtime-rule")
def read_overtime_rule(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rule = calc.get_overtime_rule(db, principal.org_id)
    db.commit()
    return {c.key: getattr(rule, c.key) for c in rule.__mapper__.column_attrs}


@router.put("/overtime-rule")
def update_overtime_rule(
    payload: OvertimeRuleIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("configure_policies")
    rule = calc.get_overtime_rule(db, principal.org_id)
    before = audit.snapshot(rule)
    for field, value in payload.model_dump().items():
        setattr(rule, field, value)
    audit.record_for(
        db, principal, request, action="overtime_rule.updated",
        entity_type="overtime_rule", entity_id=rule.id, before=before, after=rule,
    )
    db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Groups (FR-B-07)
# ---------------------------------------------------------------------------


@router.get("/groups")
def list_groups(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(UserGroup).where(UserGroup.org_id == principal.org_id)).all()
    return [{"id": r.id, "name": r.name, "member_ids": r.member_ids} for r in rows]


@router.post("/groups", status_code=201)
def create_group(
    payload: GroupIn, request: Request,
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    principal.require("manage_users")
    group = UserGroup(id=new_id(), org_id=principal.org_id, **payload.model_dump())
    db.add(group)
    audit.record_for(
        db, principal, request, action="group.created", entity_type="user_group",
        entity_id=group.id, after=group,
    )
    db.commit()
    return {"id": group.id}

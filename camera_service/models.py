from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

class PersonnelRole(str, Enum):
    OWNER = "OWNER"
    MANAGER = "MANAGER"
    WORKER = "WORKER"

class IdentityState(str, Enum):
    UNRESOLVED = "UNRESOLVED"
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"

class PersonnelCreate(BaseModel):
    employee_code: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=1, max_length=128)
    role: PersonnelRole
    phone: Optional[str] = None
    email: Optional[str] = None

class PersonnelPatch(BaseModel):
    full_name: Optional[str] = None
    role: Optional[PersonnelRole] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    active: Optional[bool] = None

class IdentitySeen(BaseModel):
    store_id: str
    camera_id: str
    track_id: str
    person_id: str
    timestamp: datetime
    confidence: float
    bbox: tuple[float, float, float, float]

class LineCrossingEvent(BaseModel):
    store_id: str
    camera_id: str
    track_id: str
    direction: str  # ENTRY or EXIT
    timestamp: datetime
    bbox: tuple[float, float, float, float]

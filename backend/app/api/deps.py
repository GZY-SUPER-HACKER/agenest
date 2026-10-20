"""Dependency wiring for settings and the database.

Both are resolved from the app state installed at startup, so a test can build the app
with its own tmp-path database without touching module-level globals.
"""


from typing import Annotated

from fastapi import Depends, Request

from ..agent.supervisor import Supervisor
from ..config import Settings
from ..database import Database
from ..stream import AgentBroker, Broker


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_broker(request: Request) -> Broker:
    return request.app.state.broker


def get_agent_broker(request: Request) -> AgentBroker:
    return request.app.state.agent_broker


def get_supervisor(request: Request) -> Supervisor:
    return request.app.state.supervisor


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Database, Depends(get_db)]
BrokerDep = Annotated[Broker, Depends(get_broker)]
AgentBrokerDep = Annotated[AgentBroker, Depends(get_agent_broker)]
SupervisorDep = Annotated[Supervisor, Depends(get_supervisor)]

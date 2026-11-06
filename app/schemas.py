import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Environment = Literal['DEV', 'INT', 'QAT', 'PROD']


class ChatInput(BaseModel):
    release_id: str = Field(min_length=1, max_length=32)
    question: str = Field(min_length=1, max_length=1000)


class ReleaseInput(BaseModel):
    profile_id: str = Field(min_length=1, max_length=32)
    year: int = Field(ge=2000, le=2200)
    quarter: int = Field(ge=1, le=4)
    number_one: str = Field(min_length=1, max_length=80, pattern=r'^\S(?:[^\r\n]*\S)?$')
    number_two: str = Field(min_length=1, max_length=80, pattern=r'^\S(?:[^\r\n]*\S)?$')
    name: str = Field(min_length=1, max_length=180)
    version: str = Field(min_length=1, max_length=80)
    current_env: Environment = 'DEV'
    change_number: str = Field(default='', pattern=r'^(CHG[0-9]+)?$')
    jira_jql: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def distinct_numbers(self):
        if self.number_one.casefold() == self.number_two.casefold():
            raise ValueError('Enter two different release numbers.')
        return self


class CloseReleaseInput(BaseModel):
    summary: str = Field(min_length=1, max_length=10000)


class AccessUserInput(BaseModel):
    principal: str = Field(pattern=r'^(local-demo|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$')
    name: str = Field(min_length=1, max_length=180)
    email: str = Field(default='', max_length=255)
    enabled: bool = True
    group_ids: list[str] = Field(default_factory=list, max_length=100)
    revision: int = Field(default=0, ge=0)

    @field_validator('principal')
    @classmethod
    def normalize_principal(cls, value):
        return value.lower()


class AccessGroupInput(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    description: str = Field(default='', max_length=2000)
    permissions: list[str] = Field(default_factory=list, max_length=50)
    profile_ids: list[str] = Field(default_factory=list, max_length=100)
    revision: int = Field(default=0, ge=0)


class ActivityInput(BaseModel):
    environment: Environment
    title: str = Field(min_length=1, max_length=500)
    category: str = Field(default='General', max_length=80)
    owner: str = Field(default='', max_length=180)
    email: str = Field(default='', max_length=255)

    @field_validator('email')
    @classmethod
    def email_address(cls, value):
        if value and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value):
            raise ValueError('Enter one valid stakeholder email address.')
        return value.lower()


class NoteInput(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class ActivityStatus(BaseModel):
    status: Literal['pending', 'in_progress', 'complete', 'blocked', 'cancelled']


class DecisionInput(BaseModel):
    decision: Literal['approved', 'rejected']
    reason: str = Field(min_length=1, max_length=2000)


class DeliveryResolution(BaseModel):
    status: Literal['sent', 'draft']
    reason: str = Field(min_length=1, max_length=2000)


class SyncInput(BaseModel):
    source: Literal['servicenow', 'jira', 'github', 'jenkins']
    environment: Environment = 'DEV'
    task_numbers: list[str] = Field(default_factory=list, max_length=200)
    repository: str = Field(default='', max_length=255, pattern=r'^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?$')
    base: str = Field(default='', max_length=200)
    head: str = Field(default='', max_length=200)
    job: str = Field(default='', max_length=255)
    build_number: int = Field(default=0, ge=0)

    @field_validator('task_numbers')
    @classmethod
    def task_ids(cls, values):
        if any(not re.fullmatch(r'CTASK[0-9]+', value) for value in values):
            raise ValueError('Task numbers must be CTASK followed by digits.')
        return values


class DeploymentInput(BaseModel):
    environment: Environment
    job: str = Field(min_length=1, max_length=255)
    build_number: int = Field(ge=1)
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', max_length=255)
    sha: str = Field(pattern=r'^([a-f0-9]{40}|[a-f0-9]{64})$')
    result: Literal['SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE']
    deployed: bool
    issue_keys: list[str] = Field(max_length=2000)
    url: str = Field(default='', max_length=1024)

    @field_validator('issue_keys')
    @classmethod
    def jira_keys(cls, values):
        if any(not re.fullmatch(r'[A-Z][A-Z0-9_]*-[0-9]+', value) for value in values):
            raise ValueError('Issue keys must be Jira keys such as PAY-123.')
        return sorted(set(values))

    @field_validator('url')
    @classmethod
    def safe_link(cls, value):
        if value and not value.startswith('https://'):
            raise ValueError('Deployment URLs must use HTTPS.')
        return value

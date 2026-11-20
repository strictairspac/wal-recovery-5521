from pydantic import BaseModel, Field


class HandoverInput(BaseModel):
    label: str = Field(min_length=2, max_length=120)
    log_set_id: int = Field(gt=0)
    crash_case_ids: list[int] = Field(min_length=1, max_length=50)

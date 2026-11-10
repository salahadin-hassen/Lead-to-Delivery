"""Shape of ``project_templates.definition`` (JSONB).

Seeded templates are validated against this model by the test suite; project
generation (a later task) will validate before creating milestones and tasks.

Day offsets are relative to the project start date.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Offset = Annotated[int, Field(ge=0, le=730)]


class TemplateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Key
    title: Text
    description: Annotated[str, StringConstraints(max_length=2000)] | None = None
    due_offset_days: Offset
    client_visible: bool = True
    assignee_role: Key | None = None


class TemplateMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Key
    name: Text
    due_offset_days: Offset
    tasks: Annotated[list[TemplateTask], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _tasks_due_by_milestone(self) -> "TemplateMilestone":
        for task in self.tasks:
            if task.due_offset_days > self.due_offset_days:
                raise ValueError(
                    f"task {task.key!r} is due after its milestone {self.key!r}"
                )
        return self


class TemplateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    default_duration_days: Annotated[int, Field(ge=1, le=730)]
    milestones: Annotated[list[TemplateMilestone], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def _consistent(self) -> "TemplateDefinition":
        milestone_keys = [m.key for m in self.milestones]
        if len(set(milestone_keys)) != len(milestone_keys):
            raise ValueError("milestone keys must be unique")
        # Task keys become tasks.template_task_key, unique per project.
        task_keys = [t.key for m in self.milestones for t in m.tasks]
        if len(set(task_keys)) != len(task_keys):
            raise ValueError("task keys must be unique across the whole template")
        offsets = [m.due_offset_days for m in self.milestones]
        if offsets != sorted(offsets):
            raise ValueError("milestones must be ordered by due_offset_days")
        if offsets[-1] > self.default_duration_days:
            raise ValueError("last milestone is due after default_duration_days")
        return self

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ColumnMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    old_column: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}$")
    new_column: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}$")
    is_key: bool = False


class PairConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    old_sheet: str | None = None
    new_sheet: str | None = None
    old_report_id: str | None = Field(default=None, max_length=100)
    new_report_id: str | None = Field(default=None, max_length=100)
    old_header_start: int = Field(default=1, ge=1, le=1048576)
    old_header_end: int = Field(default=1, ge=1, le=1048576)
    new_header_start: int = Field(default=1, ge=1, le=1048576)
    new_header_end: int = Field(default=1, ge=1, le=1048576)
    old_range: str | None = None
    new_range: str | None = None
    mode: Literal["key", "position"] = "key"
    pairing_confirmed: bool = False
    confirmation_reason: str = Field(default="", max_length=2000)
    columns: list[ColumnMapping] = Field(default_factory=list, max_length=1000)


class CompareConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairs: list[PairConfig] = Field(default_factory=list, max_length=5000)
    absolute_tolerance: str = "0"
    ignore_outer_whitespace: bool = True
    compare_formulas: bool = True
    compare_formatting: bool = False
    review_revision: int | None = Field(default=None, ge=0)

    @field_validator("absolute_tolerance")
    @classmethod
    def valid_tolerance(cls, value: str) -> str:
        try:
            number = Decimal(value)
            if not number.is_finite() or number < 0:
                raise ValueError
        except (ValueError, InvalidOperation):
            raise ValueError("金额容差必须是大于或等于零的有限数字")
        return value


class StructureCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, min_length=1, max_length=100)
    text: str = Field(max_length=10000)


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    side: Literal["old", "new"]
    action: Literal["cell_update", "cell_verify", "cell_unverify", "page_verify", "page_unverify", "table_update"]
    target_id: str | None = Field(default=None, max_length=100)
    page: int | None = Field(default=None, ge=1)
    value: str | None = Field(default=None, max_length=10000)
    reason: str = Field(min_length=1, max_length=2000)
    sheet: str | None = Field(default=None, min_length=1, max_length=1000)
    rows: list[list[StructureCell]] | None = Field(default=None, min_length=1, max_length=500)
    structure_confirmed: bool | None = None

    @model_validator(mode="after")
    def validate_structure(self):
        if self.action != "table_update":
            if self.sheet is not None or self.rows is not None or self.structure_confirmed is not None:
                raise ValueError("仅结构调整操作可以提交表格结构")
            return self
        if not self.sheet or self.page is None or not self.rows or self.structure_confirmed is not True:
            raise ValueError("结构调整需指定页面、物理表和行列内容，并确认已对照原图核对结构")
        if self.target_id is not None or self.value is not None:
            raise ValueError("结构调整不能同时提交单格核验或文本校正参数")
        width = len(self.rows[0])
        if not 1 <= width <= 60 or any(len(row) != width for row in self.rows) or len(self.rows) * width > 10000:
            raise ValueError("结构必须为矩形，最多500行、60列且不超过10000格")
        if sum(len(cell.text) for row in self.rows for cell in row) > 2000000:
            raise ValueError("单次结构调整的文字总量不能超过200万字符")
        return self

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请填写操作原因")
        return value.strip()

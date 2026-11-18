"""Evidence-preserving semantic comparison for construction cost reports.

Header aliases and the three-row E.1 header rule are adapted from PDFAnalysis
PdfToJson/python/pdftojson/cli.py: PROJECT_SUMMARY_SPEC, UNIT_SUMMARY_SPEC,
COST_SUMMARY_SPEC and GENERIC_TABLE_SPECS["fbqd"]. This local adapter retains
ReportContrast cell evidence instead of depending on an external checkout.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
import re

from openpyxl.utils import get_column_letter


_ALIASES = {
    "sequence": ("序号",),
    "code": ("项目编码", "清单编码", "编码", "编号"),
    "name": ("项目名称", "清单名称", "单项工程名称", "单位工程名称", "工程名称", "费用名称", "汇总内容", "费用项目名称", "项目", "名称"),
    "description": ("项目特征", "项目特征描述", "清单特征", "特征", "描述"),
    "unit": ("计量单位", "单位", "单位本章"),
    "quantity": ("工程量", "数量", "工程数量"),
    "unit_price": ("综合单价", "综合单价元", "单价", "单价元", "价格"),
    "amount": ("金额", "金额元", "合价", "合价元", "造价", "造价元", "最高投标限价", "最高投标限价元", "投标报价", "投标报价元", "费用", "小计"),
    "rate": ("费率", "费率%"),
    "basis": ("计算基础", "计算基数"),
    "provisional": ("暂估价", "暂估价元", "其中材料、设备暂估价", "其中材料、设备暂估价元", "材料、设备暂估价元"),
    "noncompetitive": ("不可竞争费", "不可竞争费元"),
    "labor": ("定额人工费", "人工费"),
    "machine": ("定额机械费", "机械费"),
    "safety": ("安全文明施工费", "安全文明施工费元"),
    "regulation": ("规费", "规费元"),
    "tax": ("税金", "税金元"),
}
_LABELS = {"sequence": "序号", "code": "项目编码", "name": "项目名称", "description": "项目特征", "unit": "计量单位",
           "quantity": "工程量", "unit_price": "综合单价", "amount": "金额", "rate": "费率",
           "basis": "计算基础", "provisional": "暂估价", "noncompetitive": "不可竞争费", "labor": "定额人工费",
           "machine": "定额机械费", "safety": "安全文明施工费", "regulation": "规费", "tax": "税金"}
_NUMERIC = {"quantity", "unit_price", "amount", "rate", "provisional", "noncompetitive", "labor", "machine", "safety", "regulation", "tax"}
_SUPPORTED = {"D.1", "D.2", "D.3", "E.1"}
_HEADER_SCOPE_MODIFIERS = {"其中", "定额", "人民币", "含税", "不含税"}
_GENERIC_AMOUNT_HEADERS = {"金额", "造价", "费用"}
_HEADER_PLACEHOLDERS = {"-", "—", "–", "--", "——", "/", "／", "\\"}
_HEADER_CURRENCY_OR_RATIO_UNITS = {"元", "万元", "亿元", "千元", "人民币元", "美元", "%", "％", "‰"}
_SORTED_UNITS = tuple(sorted(_HEADER_CURRENCY_OR_RATIO_UNITS, key=len, reverse=True))
_HEADER_MEASURE_UNITS = {
    "m", "m2", "m3", "㎡", "㎥", "km", "cm", "mm", "t", "kg", "g", "l",
    "个", "台", "套", "项", "组", "根", "块", "株", "座", "处",
    "工日", "台班", "延米", "平方米", "立方米", "吨", "千克", "米", "樘", "扇", "件",
}
_ALL_ALIAS_WORDS = {alias for aliases in _ALIASES.values() for alias in aliases}


def _match_alias_for_label(label, raw_text=""):
    # 先精确查 _ALIASES；未中且提供 raw_text 时仅剥除内部为合法单位的括号块后匹配；
    # 仍未中且剥除末尾货币/比例单位后非空时，仅在 _NUMERIC 字段别名中匹配（避免“单位元”等非数值列误匹配）。
    if not label:
        return None
    for field, aliases in _ALIASES.items():
        if label in aliases:
            return field
    cand = label
    if raw_text:
        compact = _compact(raw_text)
        stripped = re.sub(
            r"[（(]([^（）()]+)[）)]",
            lambda m: "" if _is_unit_token(m.group(1), allow_measure=True) else m.group(0),
            compact,
        )
        stripped_label = _header(stripped)
        if stripped_label and stripped_label != label:
            for field, aliases in _ALIASES.items():
                if stripped_label in aliases:
                    return field
            cand = stripped_label
    for unit in _SORTED_UNITS:
        if cand.endswith(unit) and len(cand) > len(unit):
            stem = cand[: -len(unit)]
            for field in _NUMERIC:
                if stem in _ALIASES[field]:
                    return field
    return None


def _text(cell):
    return str(cell.get("current_text", cell.get("raw_text", cell.get("display", cell.get("value", "")))) or "").strip()


def _compact(value):
    return re.sub(r"\s+", "", value)


def _header(value):
    return re.sub(r"[()（）:：]", "", _compact(value))


def _extract_paren_colnum(text):
    # 提取单纯括号数字列号（如 (1)、（2）），不含运算公式
    compact = _compact(text)
    match = re.fullmatch(r"[（(](\d{1,2})[）)]", compact)
    return int(match[1]) if match else None


def _is_paren_colnum(text):
    # 仅识别带括号的 1~2 位列号或列号运算公式（如 (1)、（2）、(4)=(2)+(3)、（4）＝（2）＋（3）），避免误匹配财务负数如 (100.00)
    compact = _compact(text)
    return bool(re.fullmatch(r"[（(]\d{1,2}[）)](?:[=＝+\-＋－*/×÷][（(]\d{1,2}[）)])*", compact))


def _is_paren_formula(text):
    # 识别带运算符号的列号公式（如 (4)=(2)+(3)、（4）＝（2）＋（3））
    compact = _compact(text)
    return bool(re.fullmatch(r"[（(]\d{1,2}[）)](?:[=＝+\-＋－*/×÷][（(]\d{1,2}[）)])+", compact))


def _norm_paren_formula(text):
    # 归一化全角括号、等号及加减乘除号，保留运算符语义与操作数列号供签名比对
    return (
        _compact(text)
        .replace("（", "(")
        .replace("）", ")")
        .replace("＝", "=")
        .replace("＋", "+")
        .replace("－", "-")
        .replace("×", "*")
        .replace("÷", "/")
    )


def _is_valid_paren_formula(text, col, max_col, valid_colnums, paren_items):
    # 校验列号公式中的全部括号数字必须处于 1..max_col 有效列范围；
    # 若含等号则左侧列号须不超过当前列号且符合有效列号上下文（无纯列号上下文时须严格等于当前列号）；
    # 禁止无等号的孤立算式（如 (12)-(5)、(12)+(34)）或越界列号混入表头。
    compact = _compact(text)
    if not _is_paren_formula(compact) or not max_col:
        return False
    nums = [int(n) for n in re.findall(r"[（(](\d{1,2})[）)]", compact)]
    if not nums or any(n < 1 or n > max_col for n in nums):
        return False
    norm = compact.replace("＝", "=")
    if "=" in norm:
        parts = norm.split("=")
        if len(parts) != 2:
            return False
        lhs_num = _extract_paren_colnum(parts[0])
        if lhs_num is None or lhs_num < 1 or lhs_num > col:
            return False
        if not valid_colnums:
            if paren_items:
                return False
            return lhs_num == col
        return all(
            (pnum < lhs_num if pcol < col else lhs_num < pnum)
            for pcol, pnum in paren_items
            if pcol != col
        )
    return valid_colnums


def _is_unit_token(text, *, allow_measure=True):
    compact = _compact(text)
    if not compact:
        return False
    unwrapped = re.sub(r"^[（(](.+)[）)]$", r"\1", compact)
    norm_unwrapped = _norm_unit(unwrapped)
    if unwrapped in _HEADER_CURRENCY_OR_RATIO_UNITS or norm_unwrapped in _HEADER_CURRENCY_OR_RATIO_UNITS:
        return True
    if "/" in norm_unwrapped:
        left, right = norm_unwrapped.split("/", 1)
        if left in _HEADER_CURRENCY_OR_RATIO_UNITS and right in _HEADER_MEASURE_UNITS:
            return True
    if allow_measure and (unwrapped in _HEADER_MEASURE_UNITS or norm_unwrapped in _HEADER_MEASURE_UNITS):
        return True
    return False


def _is_decorative_token(
    text,
    field=None,
    *,
    allow_paren_colnum=True,
    allow_placeholder=True,
    allow_paren_formula=None,
):
    compact = _compact(text)
    if not compact:
        return False
    if compact in _HEADER_PLACEHOLDERS:
        return allow_placeholder
    if _is_paren_formula(compact):
        return allow_paren_colnum if allow_paren_formula is None else allow_paren_formula
    if _extract_paren_colnum(compact) is not None:
        return allow_paren_colnum
    # 仅数值列或未映射列允许表头单位装饰（序号、编码、名称、特征、计量单位列严禁放行“元/万元/%”等货币或比例单位）
    if field is not None and field not in _NUMERIC:
        return False
    return _is_unit_token(compact, allow_measure=True)


def _row_decorative_flags(row, mapping=None, columns=None):
    # 表头列号校验：纯括号数字需整行 >= 2 个、至少一个在已明确映射的非数值业务列、1 <= n <= 物理列号、严格递增；
    # 取消整行含公式时对纯括号数字的全局豁免，且对公式自身严格校验全部数字在 1..max_col 与等式上下文内；
    # 占位符（如 '-'）需整行存在 >= 2 个占位符或 >= 2 个其他非占位符装饰 token，避免 1 个 '-' + 1 个单位凑数。
    max_col = columns if columns is not None else max(row.keys(), default=0)
    paren_items = []
    for col, cell in sorted(row.items()):
        num = _extract_paren_colnum(_text(cell))
        if num is not None:
            paren_items.append((col, num))
    nums = [num for _, num in paren_items]
    valid_colnums = (
        len(paren_items) >= 2
        and all(1 <= num <= col and (columns is None or num <= columns) for col, num in paren_items)
        and all(a < b for a, b in zip(nums, nums[1:]))
        and (
            not mapping
            or any(mapping.get(col) is not None and mapping.get(col) not in _NUMERIC for col, _ in paren_items)
        )
    )
    allow_paren_colnum = valid_colnums
    valid_formula_cols = {
        col
        for col, cell in row.items()
        if _is_valid_paren_formula(_text(cell), col, max_col, valid_colnums, paren_items)
    }
    placeholder_count = 0
    non_placeholder_dec_count = 0
    for col, cell in row.items():
        text = _text(cell)
        if not text:
            continue
        if _compact(text) in _HEADER_PLACEHOLDERS:
            placeholder_count += 1
            continue
        field = (mapping or {}).get(col)
        if _is_decorative_token(
            text,
            field,
            allow_paren_colnum=allow_paren_colnum,
            allow_placeholder=False,
            allow_paren_formula=(col in valid_formula_cols),
        ):
            non_placeholder_dec_count += 1
    allow_placeholder = placeholder_count >= 2 or non_placeholder_dec_count >= 2
    return allow_paren_colnum, allow_placeholder, valid_formula_cols


def _is_split_alias_prefix(text):
    # 判断文本是否属于合法业务表头别名的跨行拆字前缀（如“暂估”之于“暂估价”），排除含冒号的注记及已有完整别名
    compact = _compact(text)
    if not compact or ":" in compact or "：" in compact:
        return False
    if compact in _HEADER_PLACEHOLDERS or _is_paren_colnum(compact) or _is_unit_token(compact, allow_measure=True):
        return False
    cleaned = _header(text)
    if not cleaned or cleaned in _ALL_ALIAS_WORDS:
        return False
    return any(alias.startswith(cleaned) and len(alias) > len(cleaned) for alias in _ALL_ALIAS_WORDS)


def _norm_unit(unit):
    # 统一全角符号、面积/体积上标（㎡/m²->m2、㎥/m³->m3）及大小写，避免等价写法误报
    return (
        unit.replace("％", "%")
        .replace("／", "/")
        .replace("㎡", "m2")
        .replace("㎥", "m3")
        .replace("²", "2")
        .replace("³", "3")
        .lower()
    )


def _split_paren_blocks_and_stripped(text, *, allow_measure=True):
    # 统一解析并剥除括号内合法的单位、纯口径修饰或“修饰词+单位”复合块（如“（含税元）”“（其中：万元）”）；
    # 严禁剥除空括号或未识别备注括号（置 has_invalid=True），并检测剥除后是否残留未闭合括号（has_unclosed=True）。
    compact = _compact(text)
    if not compact:
        return set(), set(), "", False, False
    units, modifiers, has_invalid = set(), set(), False

    def _replace_block(match):
        nonlocal has_invalid
        inner = match.group(1)
        if not inner:
            has_invalid = True
            return match.group(0)
        if _is_unit_token(inner, allow_measure=allow_measure):
            units.add(_norm_unit(inner))
            return ""
        inner_cleaned = _header(inner)
        if inner_cleaned in _HEADER_SCOPE_MODIFIERS:
            modifiers.add(inner_cleaned)
            return ""
        if inner_cleaned not in _ALL_ALIAS_WORDS:
            for unit in _SORTED_UNITS:
                if inner_cleaned.endswith(unit):
                    prefix = inner_cleaned[: -len(unit)]
                    if prefix in _HEADER_SCOPE_MODIFIERS:
                        units.add(_norm_unit(unit))
                        modifiers.add(prefix)
                        return ""
        has_invalid = True
        return match.group(0)

    stripped = re.sub(r"[（(]([^（）()]*)[）)]", _replace_block, compact)
    has_unclosed = bool(re.search(r"[（()）]", stripped))
    return units, modifiers, stripped, has_invalid, has_unclosed


def _extract_header_units(text):
    # 提取表头单元格中的计量/货币/比例单位（兼容单行括号如“金额(元)”、括号前后缀如“金额元（万元）”、
    # 修饰括号掩蔽如“费用元（含税）”、复合修饰单位括号如“费用（含税元）”及独立单位行如“万元”）；
    # 存在未闭合括号碎片（如“万元）”）时禁止对外层做末尾拆解，防止误切为“元”。
    compact = _compact(text)
    if not compact:
        return set()
    if _is_unit_token(compact, allow_measure=True):
        unwrapped = re.sub(r"^[（(](.+)[）)]$", r"\1", compact)
        return {_norm_unit(unwrapped)}
    units, _, stripped, _, has_unclosed = _split_paren_blocks_and_stripped(compact, allow_measure=True)
    if not has_unclosed and stripped:
        if _is_unit_token(stripped, allow_measure=True):
            units.add(_norm_unit(stripped))
        else:
            # 循环剥除所有连续合法后缀单位（最长单位优先；若剩余词干本身即为完整单位则整体收录并终止，避免误拆“人民币元”）
            cur = _header(stripped)
            while cur:
                if _is_unit_token(cur, allow_measure=True):
                    units.add(_norm_unit(cur))
                    break
                matched_unit = next(
                    (unit for unit in _SORTED_UNITS if cur.endswith(unit) and len(cur) > len(unit)),
                    None,
                )
                if not matched_unit:
                    break
                units.add(_norm_unit(matched_unit))
                cur = cur[: -len(matched_unit)]
    return units


def _extract_header_modifiers(text):
    # 提取独立口径修饰词或括号内外口径修饰（如“其中”“定额”“人民币”“含税”“不含税”），
    # 剥除合法单位/修饰括号后再匹配外层修饰词，并保留完整别名防拆解保护（如不拆“定额人工费”）。
    compact = _compact(text)
    if not compact:
        return set()
    _, modifiers, stripped, has_invalid, has_unclosed = _split_paren_blocks_and_stripped(compact, allow_measure=True)
    cleaned_full = _header(compact)
    cleaned_stripped = _header(stripped)
    if (
        has_invalid
        or has_unclosed
        or not cleaned_stripped
        or cleaned_full in _ALL_ALIAS_WORDS
        or cleaned_stripped in _ALL_ALIAS_WORDS
    ):
        return modifiers
    base = cleaned_stripped
    while base:
        if _is_unit_token(base, allow_measure=True):
            break
        matched_unit = next(
            (unit for unit in _SORTED_UNITS if base.endswith(unit) and len(base) > len(unit)),
            None,
        )
        if not matched_unit:
            break
        base = base[: -len(matched_unit)]
    if base in _HEADER_SCOPE_MODIFIERS:
        modifiers.add(base)
    return modifiers


def _is_pure_modifier_or_unit_token(text, *, allow_measure=False):
    # 严格判定文本是否纯粹由合法单位或“作用域修饰词 + 可选货币/比例单位”构成，
    # 允许“其中：（元）”“（含税）”“含税（含税元）”等纯组合，拒绝“未知说明（含税）”“含税说明”及孤立/未闭合括号。
    compact = _compact(text)
    if not compact:
        return False
    if _is_unit_token(compact, allow_measure=allow_measure):
        return True
    units, modifiers, stripped, has_invalid, has_unclosed = _split_paren_blocks_and_stripped(
        compact, allow_measure=allow_measure
    )
    if has_invalid or has_unclosed:
        return False
    if not stripped:
        return bool(units or modifiers)
    if _is_unit_token(stripped, allow_measure=allow_measure):
        return True
    cleaned = _header(stripped)
    return bool(cleaned) and (
        cleaned in _HEADER_SCOPE_MODIFIERS
        or any(cleaned.endswith(u) and cleaned[: -len(u)] in _HEADER_SCOPE_MODIFIERS for u in _SORTED_UNITS)
    )


def _is_generic_amount_parent_text(text):
    # 判断文本去掉合法单位/修饰括号与末尾单位后是否属于跨列金额父表头（如“金额”“费用（元）”“造价（万元）”），
    # 严禁剥除含未知备注的括号（如“金额（注：不含规费）”）或未闭合括号。
    compact = _compact(text)
    if not compact:
        return False
    _, _, stripped, has_invalid, has_unclosed = _split_paren_blocks_and_stripped(compact, allow_measure=False)
    if has_invalid or has_unclosed:
        return False
    cleaned = _header(stripped)
    return cleaned in _GENERIC_AMOUNT_HEADERS or any(
        cleaned.endswith(u) and cleaned[: -len(u)] in _GENERIC_AMOUNT_HEADERS for u in _SORTED_UNITS
    )


def _is_spanning_amount_fragment(text, *, allow_placeholder=False):
    # 统一跨列金额父表头允许的原子碎片判定口径，避免多处校验口径分裂
    compact = _compact(text)
    if not compact:
        return True
    if allow_placeholder and compact in _HEADER_PLACEHOLDERS:
        return True
    return (
        _is_generic_amount_parent_text(text)
        or compact in {"（", "）", "(", ")"}
        or _is_pure_modifier_or_unit_token(text, allow_measure=False)
    )


def _is_hejia_label(text):
    compact = _compact(text)
    if not compact:
        return False
    stripped = re.sub(
        r"[（(]([^（）()]+)[）)]",
        lambda m: "" if _is_unit_token(m.group(1), allow_measure=False) else m.group(0),
        compact,
    )
    return _header(stripped) in {"合价", "合价元"}


def _unit_currency_base(unit):
    # 提取单位中的货币或比例基准（如 '元/m2' -> '元'，'万元/m2' -> '万元'）
    return unit.split("/", 1)[0]


def _has_unit_conflict(units):
    # 校验同一单位集合中是否存在多货币基准冲突，或含复合分母（如元/m2）时混用多种不同单位
    if not units:
        return False
    bases = {_unit_currency_base(u) for u in units}
    has_denom = any("/" in u for u in units)
    return len(bases) > 1 or (has_denom and len(units) > 1)


def _coalesce_split_bracket_texts(texts):
    # 将同一列内跨行拆开的未闭合括号片段按括号平衡合并为完整字符串，避免单位/修饰提取漏采或错切；
    # 若扫描结束仍残留未闭合片段，则原样退回逐格文本。
    result, buf, balance = [], [], 0
    for text in texts:
        comp = _compact(text)
        if not comp:
            continue
        opens = comp.count("（") + comp.count("(")
        closes = comp.count("）") + comp.count(")")
        if buf:
            buf.append(comp)
            balance += opens - closes
            if balance <= 0:
                result.append("".join(buf))
                buf.clear()
                balance = 0
        elif opens > closes:
            buf.append(comp)
            balance = opens - closes
        else:
            result.append(text)
    if buf:
        result.extend(buf)
    return result


def _is_standalone_header_unit_piece(text):
    # 判断单个表头片段（或已闭合括号合并片段）自身是否已完整可解释，避免误粘连各自独立的合法字段行
    comp = _compact(text)
    if not comp:
        return True
    if (
        comp in _HEADER_PLACEHOLDERS
        or comp in {"（", "）", "(", ")"}
        or _extract_paren_colnum(comp) is not None
        or _is_paren_formula(comp)
        or _is_unit_token(comp, allow_measure=True)
        or _is_pure_modifier_or_unit_token(text, allow_measure=False)
        or _is_generic_amount_parent_text(text)
    ):
        return True
    _, _, stripped, has_invalid, has_unclosed = _split_paren_blocks_and_stripped(text, allow_measure=True)
    return (
        not has_invalid
        and not has_unclosed
        and bool(_header(stripped))
        and _match_alias_for_label(_header(text), text) is not None
    )


def _coalesce_split_header_texts(texts):
    # 先按括号平衡拼合跨行拆括号片段，再对含无法单独解释的跨行拆字碎片（如“金额万”+“元”、“金额（元）万”+“元”）
    # 按与列校验同构的 2~3 行连续拼合规则合并，防止跨行单位截断漏采。
    items = _coalesce_split_bracket_texts(texts)
    if len(items) < 2:
        return items
    standalone = [_is_standalone_header_unit_piece(item) for item in items]
    result = []
    i = 0
    while i < len(items):
        merged_piece = None
        merged_span = 1
        for span in (2, 3):
            end_idx = i + span
            if end_idx > len(items):
                continue
            if all(standalone[i:end_idx]):
                continue
            parts = items[i:end_idx]
            if any(
                ":" in _compact(p)
                or "：" in _compact(p)
                or _compact(p) in _HEADER_PLACEHOLDERS
                or _is_paren_colnum(_compact(p))
                or _is_paren_formula(_compact(p))
                for p in parts
            ):
                continue
            if any(
                (lambda s: s[3] or s[4])(_split_paren_blocks_and_stripped(p, allow_measure=True))
                for p in parts
            ):
                continue
            joined_comp = "".join(_compact(p) for p in parts)
            _, _, stripped_joined, has_invalid, has_unclosed = _split_paren_blocks_and_stripped(
                joined_comp, allow_measure=True
            )
            if has_invalid or has_unclosed or not _header(stripped_joined):
                continue
            if (
                _is_unit_token(joined_comp, allow_measure=True)
                or _is_unit_token(stripped_joined, allow_measure=True)
                or _is_pure_modifier_or_unit_token(joined_comp, allow_measure=False)
                or _is_generic_amount_parent_text(joined_comp)
                or _match_alias_for_label(_header(joined_comp), joined_comp) is not None
            ):
                merged_piece = joined_comp
                merged_span = span
                break
        if merged_piece is not None:
            result.append(merged_piece)
            i += merged_span
        else:
            result.append(items[i])
            i += 1
    return result


def _collect_header_col_units_mods(texts):
    # 先拼合同一列内跨行拆括号及跨行拆字单位片段，再统一提取单位与口径修饰集合
    units, modifiers = set(), set()
    for text in _coalesce_split_header_texts(texts):
        units.update(_extract_header_units(text))
        modifiers.update(_extract_header_modifiers(text))
    return units, modifiers


def _is_valid_mapped_header_column_cells(rows, col, field, header_end, header_row_flags):
    # 核验已映射列在 1..header_end 的全部非空表头单元格：
    # 每个单元格必须要么本身为合法装饰/纯修饰/泛化金额父表头/完整括号字段别名，要么与相邻单元格整段拼合后括号闭合且内容合法；
    # 严禁前序非法备注或未知限定文本在映射阶段被子行静默覆盖吞没。
    col_cells = [
        (r, _text(rows.get(r, {}).get(col, {})))
        for r in sorted(rows)
        if 1 <= r <= header_end and _text(rows.get(r, {}).get(col, {}))
    ]
    if not col_cells:
        return True
    explained = [False] * len(col_cells)
    for idx, (r, text) in enumerate(col_cells):
        allow_colnum, allow_ph, valid_fcols = header_row_flags[r]
        if _is_decorative_token(
            text,
            field,
            allow_paren_colnum=allow_colnum,
            allow_placeholder=allow_ph,
            allow_paren_formula=(col in valid_fcols),
        ):
            explained[idx] = True
            continue
        if _compact(text) in {"（", "）", "(", ")"}:
            explained[idx] = True
            continue
        # 非数值列严禁放行纯单位或含单位修饰词（与 _is_decorative_token 保持同一口径）
        if _is_pure_modifier_or_unit_token(text, allow_measure=False) and (
            field in _NUMERIC or not _extract_header_units(text)
        ):
            explained[idx] = True
            continue
        if field in ("unit_price", "amount") and _is_generic_amount_parent_text(text):
            explained[idx] = True
            continue
        _, _, stripped, has_invalid, has_unclosed = _split_paren_blocks_and_stripped(text, allow_measure=True)
        if (
            not has_invalid
            and not has_unclosed
            and _header(stripped)
            and (field in _NUMERIC or not _extract_header_units(text))
            and _match_alias_for_label(_header(text), text) == field
        ):
            explained[idx] = True
            continue

    for start in range(len(col_cells)):
        for span in (2, 3):
            end_idx = start + span
            if end_idx > len(col_cells):
                continue
            if all(explained[start:end_idx]):
                continue
            parts = [col_cells[k][1] for k in range(start, end_idx)]
            if any(
                ":" in _compact(p)
                or "：" in _compact(p)
                or _compact(p) in _HEADER_PLACEHOLDERS
                or _is_paren_colnum(_compact(p))
                for p in parts
            ):
                continue
            # 被单格明确拒绝的已闭合纯单位/修饰片段（如非数值列前的“（元）”）严禁借跨行拼合洗白
            if any(
                not explained[k]
                and not _split_paren_blocks_and_stripped(col_cells[k][1], allow_measure=True)[4]
                and _is_pure_modifier_or_unit_token(col_cells[k][1], allow_measure=False)
                for k in range(start, end_idx)
            ):
                continue
            joined_comp = "".join(_compact(p) for p in parts)
            _, _, stripped_joined, has_invalid, has_unclosed = _split_paren_blocks_and_stripped(
                joined_comp, allow_measure=True
            )
            if has_invalid or has_unclosed or not _header(stripped_joined):
                continue
            if field not in _NUMERIC and _extract_header_units(joined_comp):
                continue
            if _match_alias_for_label(_header(joined_comp), joined_comp) == field or (
                field in ("unit_price", "amount") and _is_generic_amount_parent_text(joined_comp)
            ):
                for k in range(start, end_idx):
                    explained[k] = True
    return all(explained)


def _analyze_header_signature(rows, mapping, header_end, max_column):
    # 生成表头语义签名并返回跨列消费的未映射列集合：
    # 包含各业务字段单位/口径、1..header_end 全列核验后的跨列父表头隔离与冲突检测、剩余未映射列及无盲区字段绑定公式签名。
    field_units = {field: set() for field in set(mapping.values())}
    field_modifiers = {field: set() for field in set(mapping.values())}
    consumed_unmapped_cols = set()
    body_populated_cols = {
        col
        for r, cells in rows.items()
        if r > header_end
        for col, cell in cells.items()
        if _text(cell)
    }

    c_up = next((c for c, f in mapping.items() if f == "unit_price"), None)
    c_am = next((c for c, f in mapping.items() if f == "amount"), None)
    sub_row = None
    if c_up is not None and c_am is not None:
        sub_row = next(
            (
                r
                for r in sorted(rows)
                if 1 <= r <= header_end
                and (
                    _header(_text(rows.get(r, {}).get(c_up, {}))) in _ALIASES["unit_price"]
                    or _is_hejia_label(_text(rows.get(r, {}).get(c_am, {})))
                )
            ),
            None,
        )

    hdr_rows = [r for r in sorted(rows) if 1 <= r <= header_end]
    # 1. 识别跨列金额父表头（仅限于 unit_price..amount 连续区间及紧密闭合的拆分括号结构），
    # 候选未映射列按 1..header_end 整列核验后，统一遍历 1..header_end 全部行提取单位与修饰。
    spanning_parent_rows = set()
    spanning_amount_units = set()
    spanning_amount_modifiers = set()
    if sub_row is not None and sub_row > 1:
        lo, hi = min(c_up, c_am), max(c_up, c_am)
        candidate_spanning_unmapped = set()
        row_span_candidates = []

        def _is_other_mapped(c):
            return c in mapping and mapping[c] not in {"unit_price", "amount"}

        def _is_bare_parent(t):
            return (
                _is_generic_amount_parent_text(t)
                and not _extract_header_units(t)
                and not re.search(r"[（()）]", _compact(t))
            )

        def _collect_bracket_unit_tail(row, start_col, already_open):
            # 从 start_col 向右收集紧邻“金额”之后的拆分“（ 单位/修饰 ）”片段；仅在括号闭合时返回含中间空列的切片。
            tail, pending_blanks = [], []
            in_parens, closed = already_open, False
            for c in range(start_col, max_column + 1):
                if _is_other_mapped(c) or c in body_populated_cols:
                    break
                t = _text(row.get(c, {}))
                comp = _compact(t)
                if not t:
                    pending_blanks.append(c)
                    continue
                if not in_parens:
                    if comp in {"（", "("}:
                        in_parens = True
                        tail.extend(pending_blanks)
                        pending_blanks.clear()
                        tail.append(c)
                        continue
                    break
                if comp in {"）", ")"}:
                    tail.extend(pending_blanks)
                    tail.append(c)
                    closed = True
                    break
                if _is_pure_modifier_or_unit_token(t, allow_measure=False):
                    tail.extend(pending_blanks)
                    pending_blanks.clear()
                    tail.append(c)
                    continue
                break
            return tail if closed else []

        for r in hdr_rows:
            if r >= sub_row or any(_is_other_mapped(c) for c in range(lo, hi + 1)):
                continue
            row = rows.get(r, {})
            in_range_non_empty = [(c, _text(row.get(c, {}))) for c in range(lo, hi + 1) if _text(row.get(c, {}))]
            span_cols = []
            if any(_is_generic_amount_parent_text(t) for _, t in in_range_non_empty):
                if not all(_is_spanning_amount_fragment(t, allow_placeholder=False) for _, t in in_range_non_empty):
                    continue
                span_cols = [c for c, _ in in_range_non_empty]
                joined_in_range = "".join(_compact(t) for _, t in in_range_non_empty)
                open_parens = joined_in_range.count("（") + joined_in_range.count("(")
                close_parens = joined_in_range.count("）") + joined_in_range.count(")")
                has_unclosed_paren = open_parens > close_parens
                if has_unclosed_paren or (open_parens == 0 and _is_bare_parent(in_range_non_empty[-1][1])):
                    span_cols.extend(_collect_bracket_unit_tail(row, hi + 1, already_open=has_unclosed_paren))
            elif not in_range_non_empty:
                for c in range(hi + 1, max_column + 1):
                    if _is_other_mapped(c) or c in body_populated_cols:
                        break
                    t = _text(row.get(c, {}))
                    if not t:
                        continue
                    if _is_generic_amount_parent_text(t):
                        span_cols = [c] + (_collect_bracket_unit_tail(row, c + 1, already_open=False) if _is_bare_parent(t) else [])
                    break

            if span_cols:
                row_unmapped = [c for c in span_cols if c not in mapping and c not in body_populated_cols and _text(row.get(c, {}))]
                row_mapped_parent = [c for c in span_cols if c in (c_up, c_am) and _text(row.get(c, {}))]
                if row_unmapped or row_mapped_parent:
                    candidate_spanning_unmapped.update(row_unmapped)
                    row_span_candidates.append((r, row_mapped_parent, row_unmapped))

        # 对候选未映射列按 1..header_end 全列校验：sub_row 之上仅限金额父碎片，sub_row 及以下仅限单位/修饰/单括号/占位符
        valid_unmapped_spanning = set()
        for c in candidate_spanning_unmapped:
            ok = True
            for r in hdr_rows:
                t = _text(rows.get(r, {}).get(c, {}))
                if not t:
                    continue
                comp = _compact(t)
                valid_cell = (
                    _is_spanning_amount_fragment(t, allow_placeholder=False)
                    if r < sub_row
                    else (
                        _is_pure_modifier_or_unit_token(t, allow_measure=False)
                        or comp in {"（", "）", "(", ")"}
                        or comp in _HEADER_PLACEHOLDERS
                    )
                )
                if not valid_cell:
                    ok = False
                    break
            if ok:
                valid_unmapped_spanning.add(c)

        for r, row_mapped_parent, row_unmapped in row_span_candidates:
            if all(c in valid_unmapped_spanning for c in row_unmapped):
                spanning_parent_rows.add(r)
                for c in row_mapped_parent:
                    t = _text(rows.get(r, {}).get(c, {}))
                    if t:
                        spanning_amount_units.update(_extract_header_units(t))
                        spanning_amount_modifiers.update(_extract_header_modifiers(t))
                consumed_unmapped_cols.update(row_unmapped)
        # 对所有已消费未映射列统一先按括号平衡拼合跨行拆括号片段，再提取单位与修饰，彻底消除作用域割裂漏采
        for c in consumed_unmapped_cols:
            col_units, col_mods = _collect_header_col_units_mods(
                _text(rows.get(r, {}).get(c, {})) for r in hdr_rows
            )
            spanning_amount_units.update(col_units)
            spanning_amount_modifiers.update(col_mods)

    # 提取各已映射列自身表头行的单位与口径修饰（跳过已归入跨列金额父表头行的 c_up/c_am，避免层级混杂）
    for col, field in mapping.items():
        col_units, col_mods = _collect_header_col_units_mods(
            _text(rows.get(r, {}).get(col, {}))
            for r in hdr_rows
            if not (r in spanning_parent_rows and col in (c_up, c_am))
        )
        field_units[field].update(col_units)
        field_modifiers[field].update(col_mods)

    # 父/子单位及口径层级校验与继承（含父表头自身多货币/含税口径内部冲突校验）
    spanning_conflict = False
    if spanning_amount_units:
        parent_bases = {_unit_currency_base(u) for u in spanning_amount_units}
        parent_has_denom = any("/" in u for u in spanning_amount_units)
        if len(parent_bases) > 1 or (parent_has_denom and len(spanning_amount_units) > 1):
            spanning_conflict = True
        for f in ("unit_price", "amount"):
            if f not in field_units:
                continue
            child_units = field_units[f]
            if child_units:
                child_bases = {_unit_currency_base(u) for u in child_units}
                if child_bases != parent_bases or (parent_has_denom and child_units != spanning_amount_units):
                    spanning_conflict = True
            else:
                field_units[f] = set(spanning_amount_units)
    if spanning_amount_modifiers:
        if {"含税", "不含税"} <= spanning_amount_modifiers:
            spanning_conflict = True
        for f in ("unit_price", "amount"):
            if f not in field_modifiers:
                continue
            child_mods = field_modifiers[f]
            if child_mods:
                if {"含税", "不含税"} <= (child_mods | spanning_amount_modifiers) and child_mods != spanning_amount_modifiers:
                    spanning_conflict = True
                else:
                    field_modifiers[f] = child_mods | spanning_amount_modifiers
            else:
                field_modifiers[f] = set(spanning_amount_modifiers)

    # 2. 将无独立单位且紧邻覆盖右侧已映射子列的未映射纯分组修饰列（如“其中”“定额”）关联到对应子字段
    modifier_conflict = False
    for pcol in range(1, max_column + 1):
        if pcol in mapping or pcol in consumed_unmapped_cols or pcol in body_populated_cols:
            continue
        col_items = [(r, _text(rows.get(r, {}).get(pcol, {}))) for r in hdr_rows if _text(rows.get(r, {}).get(pcol, {}))]
        if not col_items:
            continue
        units, mods = _collect_header_col_units_mods(t for _, t in col_items)
        if units or not mods or not all(_is_pure_modifier_or_unit_token(t, allow_measure=False) for _, t in col_items):
            continue
        if {"含税", "不含税"} <= mods:
            modifier_conflict = True
        r_mod = max(r for r, _ in col_items)
        if r_mod >= header_end:
            continue
        covered_fields = []
        for ccol in range(pcol + 1, max_column + 1):
            if any(_text(rows.get(r, {}).get(ccol, {})) for r in range(1, r_mod + 1)):
                break
            if ccol in mapping:
                covered_fields.append(mapping[ccol])
            elif ccol in body_populated_cols:
                covered_fields = []
                break
        if covered_fields:
            for field in covered_fields:
                field_modifiers[field].update(mods)
            consumed_unmapped_cols.add(pcol)

    # 3. 收集未被任何子列消费的剩余未映射列单位/修饰（去掉绝对物理列号，兼容中间空白合并列增减）
    unmapped = []
    for col in range(1, max_column + 1):
        if col in mapping or col in consumed_unmapped_cols:
            continue
        units, mods = _collect_header_col_units_mods(
            _text(rows.get(r, {}).get(col, {})) for r in hdr_rows
        )
        if units or mods:
            unmapped.append((frozenset(units), frozenset(mods)))

    # 统一校验所有已映射字段及未映射列自身的单位冲突与“含税/不含税”口径冲突，防止同构自相矛盾或互换抵消假报 unchanged
    unit_conflict = any(_has_unit_conflict(u) for u in field_units.values()) or any(
        _has_unit_conflict(u) for u, _ in unmapped
    )
    modifier_conflict = (
        modifier_conflict
        or any({"含税", "不含税"} <= m for m in field_modifiers.values())
        or any({"含税", "不含税"} <= m for _, m in unmapped)
    )

    # 4. 单遍收集显式列号（含纯括号列号及等式左值），无盲区绑定业务字段语义与物理列号，冲突或无法唯一对应即置 formula_conflict
    explicit_num_to_cols = {}
    col_to_explicit_nums = {}
    formula_conflict = False
    parsed_rows = []
    unmapped_formula_idx = 0
    for r in hdr_rows:
        row = rows.get(r, {})
        paren_items, row_formulas = [], []
        for col in sorted(row):
            text = _text(row[col])
            if not text:
                continue
            num = _extract_paren_colnum(text)
            if num is not None:
                explicit_num_to_cols.setdefault(num, set()).add(col)
                col_to_explicit_nums.setdefault(col, set()).add(num)
                paren_items.append((col, num))
            elif _is_paren_formula(text):
                norm = _norm_paren_formula(text)
                if "=" in norm:
                    lhs_num = _extract_paren_colnum(norm.split("=", 1)[0])
                    if lhs_num is not None:
                        explicit_num_to_cols.setdefault(lhs_num, set()).add(col)
                        col_to_explicit_nums.setdefault(col, set()).add(lhs_num)
                f_key = mapping[col] if col in mapping else f"unmapped:{unmapped_formula_idx}"
                if col not in mapping:
                    unmapped_formula_idx += 1
                row_formulas.append((col, f_key, norm))
        parsed_rows.append((paren_items, row_formulas))

    if any(len(nums) > 1 for nums in col_to_explicit_nums.values()) or any(
        len(cols) > 1 for cols in explicit_num_to_cols.values()
    ):
        formula_conflict = True
    unique_num_to_col = {num: next(iter(cols)) for num, cols in explicit_num_to_cols.items() if len(cols) == 1}
    no_col_offset = all(len(cols) == 1 and next(iter(cols)) == num for num, cols in explicit_num_to_cols.items())
    occupied_cols = {c for cols in explicit_num_to_cols.values() for c in cols}

    colnums, formulas = [], []
    for paren_items, row_formulas in parsed_rows:
        row_num_to_col = dict(unique_num_to_col)
        for pcol, pnum in paren_items:
            row_num_to_col[pnum] = pcol
        for col, f_key, norm in row_formulas:
            local_num_to_col = dict(row_num_to_col)
            if "=" in norm:
                lhs_num = _extract_paren_colnum(norm.split("=", 1)[0])
                if lhs_num is not None and lhs_num in explicit_num_to_cols and explicit_num_to_cols[lhs_num] != {col}:
                    formula_conflict = True

            def _bind_colnum(match, _col=col, _f_key=f_key, _map=local_num_to_col):
                nonlocal formula_conflict
                n = int(match.group(1))
                if n in _map:
                    tcol = _map[n]
                elif n not in explicit_num_to_cols and no_col_offset and 1 <= n <= max_column and n not in occupied_cols:
                    tcol = n
                else:
                    formula_conflict = True
                    return f"(unbound:{n})"
                if tcol in mapping:
                    return f"(field:{mapping[tcol]})"
                if tcol == _col:
                    return f"(self:{_f_key})"
                return f"(unmapped:{tcol}:{n})"

            formulas.append((f_key, re.sub(r"\((\d{1,2})\)", _bind_colnum, norm)))
        if paren_items:
            mapped_fields = frozenset(mapping[col] for col, _ in paren_items if col in mapping)
            unmapped_count = sum(1 for col, _ in paren_items if col not in mapping)
            colnums.append((mapped_fields, unmapped_count, tuple(num for _, num in paren_items)))
    return (
        {
            "field_units": {k: frozenset(v) for k, v in field_units.items()},
            "field_modifiers": {k: frozenset(v) for k, v in field_modifiers.items()},
            "unmapped": tuple(unmapped),
            "colnums": tuple(colnums),
            "formulas": tuple(sorted(formulas)),
            "spanning_conflict": spanning_conflict,
            "unit_conflict": unit_conflict,
            "modifier_conflict": modifier_conflict,
            "formula_conflict": formula_conflict,
        },
        consumed_unmapped_cols,
    )


def _is_parent_header_modifier(text):
    # 仅用于表头扩张：判断未映射列的前序表头是否为空、纯单位、泛化金额父表头（含“费用（元）”“造价（万元）”等）
    # 或作用域修饰词；严禁放行非空纯标点（如“((()))”）及备注文本。
    if not _compact(text):
        return True
    if not _header(text):
        return False
    return _is_pure_modifier_or_unit_token(text, allow_measure=True) or _is_generic_amount_parent_text(text)


def _is_header_decorative_row(row, mapping, columns=None, *, row_flags=None):
    # 至少含一个非空单元格，且全部非空单元格均属于合法表头装饰 token（排除孤立财务负数、倒序/超列号括号数、越界公式与单占位符凑数）
    allow_paren_colnum, allow_placeholder, valid_formula_cols = row_flags or _row_decorative_flags(row, mapping, columns)
    has_non_empty = False
    for col, cell in row.items():
        text = _text(cell)
        if not text:
            continue
        has_non_empty = True
        if not _is_decorative_token(
            text,
            mapping.get(col),
            allow_paren_colnum=allow_paren_colnum,
            allow_placeholder=allow_placeholder,
            allow_paren_formula=(col in valid_formula_cols),
        ):
            return False
    return has_non_empty


def _is_header_cell_text(
    rows,
    header_end,
    end,
    col,
    field,
    text,
    *,
    mapping=None,
    allow_paren_colnum=True,
    allow_placeholder=True,
    allow_paren_formula=True,
):
    # 判断已映射列在后续候选行中的非空内容是否仍属于合法表头词或表头装饰；
    # 严禁放行非空纯标点（如“((()))”）或跨数值字段泛化放行（如 amount 列下出现“暂估价”“人工费”“小计”）。
    if _is_decorative_token(
        text,
        field,
        allow_paren_colnum=allow_paren_colnum,
        allow_placeholder=allow_placeholder,
        allow_paren_formula=allow_paren_formula,
    ):
        return True
    cleaned = _header(text)
    if not cleaned:
        return False
    prev_texts = [
        _text(rows.get(r, {}).get(col, {}))
        for r in sorted(rows)
        if header_end is not None and 1 <= r <= header_end
    ] if rows is not None else []
    non_empty_prev = [_header(t) for t in prev_texts if _header(t)]
    if cleaned in non_empty_prev:
        return True
    # 兼容跨行拆字表头（例如上行“项目”+下行“名称”拼成“项目名称”，或“金额”+“元”拼成“金额元”）；
    # 若当前行恰好闭合前序未闭合括号且拼合后为合法字段别名（如“综合单价（”+“元/m2）”），亦无状态放行。
    if rows is not None and end is not None:
        win_raws = [
            _text(rows.get(r, {}).get(col, {}))
            for r in sorted(rows)
            if max(1, end - 2) <= r <= end and _text(rows.get(r, {}).get(col, {}))
        ]
        joined = "".join(_header(t) for t in win_raws)
        if _match_alias_for_label(joined) == field:
            return True
        if len(win_raws) >= 2 and win_raws[-1] == text:
            for start in range(len(win_raws) - 1):
                prev_comp = "".join(_compact(t) for t in win_raws[start:-1])
                if (prev_comp.count("（") + prev_comp.count("(")) > (prev_comp.count("）") + prev_comp.count(")")):
                    joined_raw = prev_comp + _compact(text)
                    if ":" not in joined_raw and "：" not in joined_raw:
                        _, _, stripped_raw, has_inv, has_unc = _split_paren_blocks_and_stripped(
                            joined_raw, allow_measure=True
                        )
                        if (
                            not has_inv
                            and not has_unc
                            and _header(stripped_raw)
                            and _match_alias_for_label(_header(joined_raw), joined_raw) == field
                        ):
                            return True
    if not non_empty_prev and _match_alias_for_label(cleaned, text) == field:
        return True
    # 仅当上行是泛化金额父表头（如“金额”“造价”“费用（元）”）时：
    # - 允许本列同义细化为“合价”/“合价元”；
    # - 或仅当同行存在另一未映射列承接“合价”（真正拆出 unit_price + amount 双子列）时，允许本列细化为 unit_price 别名；
    # 严禁在单列 amount 下未拆双子列时放行“综合单价”或“小计”。
    if field == "amount" and any(_is_generic_amount_parent_text(t) for t in prev_texts if t):
        if _is_hejia_label(text):
            return True
        if _match_alias_for_label(cleaned, text) == "unit_price" and rows is not None and end is not None:
            cand_row = rows.get(end, {})
            if any(
                other_col != col
                and (mapping is None or other_col not in mapping)
                and _is_hejia_label(_text(cell))
                for other_col, cell in cand_row.items()
            ):
                return True
    return False


def _code(report):
    code = _compact(report.get("type_code") or "").upper().replace("．", ".")
    if code:
        return code
    match = re.match(r"^(?:表)?([DE][.．]\d+)(?!\d|[.．]\d)", _compact(report.get("title", "")), re.I)
    return match[1].upper().replace("．", ".") if match else None


def _number(text):
    if not text:
        return None
    text = _compact(text).replace("，", ",")
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        raise ValueError("不是可确认的十进制数值")
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("不是可确认的十进制数值") from exc
    return value


def _source(sheet, cell):
    return {"sheet": sheet["name"], "cell": cell.get("address", ""), **cell.get("pdf_source", {})}


def _is_data_row(row, mapping, rows=None, header_end=None, end=None, columns=None, *, row_flags=None):
    # 已建立合法表头后，若下一行在序号列为纯数字序号（排除括号列号）、数值列可解析为 Decimal、
    # 或任一已映射列出现非表头/非装饰的业务内容，立即停止扩张表头。
    allow_paren_colnum, allow_placeholder, valid_formula_cols = row_flags or _row_decorative_flags(row, mapping, columns)
    for col, field in mapping.items():
        text = _text(row.get(col, {}))
        if not text:
            continue
        if field == "sequence" and re.fullmatch(r"\d+(?:\.\d+)*", _compact(text)):
            return True
        if field in _NUMERIC:
            try:
                if _number(text) is not None:
                    return True
            except ValueError:
                pass
        if not _is_header_cell_text(
            rows,
            header_end,
            end,
            col,
            field,
            text,
            mapping=mapping,
            allow_paren_colnum=allow_paren_colnum,
            allow_placeholder=allow_placeholder,
            allow_paren_formula=(col in valid_formula_cols),
        ):
            return True
    return False


def _unmapped_prev_allows_continuation(prev_texts, has_pending_split=False):
    # 统一判断未映射列的前序非空文本是否全部属于父分组修饰/表头装饰/挂起拆字前缀
    return all(
        _is_parent_header_modifier(t)
        or _extract_paren_colnum(t) is not None
        or _compact(t) in _HEADER_PLACEHOLDERS
        or _is_paren_formula(t)
        or (has_pending_split and _is_split_alias_prefix(t))
        for t in prev_texts
    )


def _is_valid_header_continuation_row(
    rows, header_end, row, mapping, columns=None, pending_unmapped_cols=None, *, row_flags=None
):
    # 核验未映射列的非空单元格均合法且前序表头允许延续（无未映射非空单元格时返回 False）
    allow_paren_colnum, allow_placeholder, valid_formula_cols = row_flags or _row_decorative_flags(row, mapping, columns)
    has_unmapped_non_empty = False
    for col, cell in row.items():
        text = _text(cell)
        if not text or col in mapping:
            continue
        has_unmapped_non_empty = True
        is_dec = _is_decorative_token(
            text,
            field=None,
            allow_paren_colnum=allow_paren_colnum,
            allow_placeholder=allow_placeholder,
            allow_paren_formula=(col in valid_formula_cols),
        )
        is_mod = _is_parent_header_modifier(text)
        prev_texts = [
            _text(rows.get(r, {}).get(col, {}))
            for r in sorted(rows)
            if header_end is not None and 1 <= r <= header_end and _text(rows.get(r, {}).get(col, {}))
        ]
        is_split = False
        if prev_texts:
            has_pending_split = bool(
                pending_unmapped_cols and any(k == "split" for k, _ in pending_unmapped_cols.get(col, ()))
            )
            if not _unmapped_prev_allows_continuation(prev_texts, has_pending_split):
                return False
            # 前序全为纯装饰词（如“元”）时，不允许后续出现“定额”“其中”等非装饰修饰词
            if all(
                _is_decorative_token(t, field=None, allow_paren_colnum=True, allow_placeholder=True, allow_paren_formula=True)
                for t in prev_texts
            ) and not is_dec:
                return False
            has_group_mod = any(
                _is_parent_header_modifier(t) and not _is_unit_token(t, allow_measure=True)
                for t in prev_texts
            )
            is_split = has_group_mod and _is_split_alias_prefix(text)
        if not (is_dec or is_mod or is_split):
            return False
    return has_unmapped_non_empty


def _deduplicate_spanning_amount(mapping, rows, header_end):
    # 对合并后的映射执行跨列金额父表头幻影消重（基于 header_end 之后的表体数据）
    result = dict(mapping)
    for field, count in Counter(result.values()).items():
        if count < 2 or field != "amount":
            continue
        candidates = [col for col, value in result.items() if value == field]
        populated = [col for col in candidates if any(_text(cells.get(col, {})) for r, cells in rows.items() if r > header_end)]
        if len(populated) != 1:
            continue
        pop_col = populated[0]
        sub_row = next(
            (r for r in sorted(rows) if r <= header_end and _is_hejia_label(_text(rows.get(r, {}).get(pop_col, {})))),
            None,
        )
        if sub_row is None or "unit_price" not in result.values():
            continue
        empty_cols = [col for col in candidates if col != pop_col]
        if all(
            any(
                _is_generic_amount_parent_text(_text(rows.get(r_p, {}).get(ecol, {})))
                and (
                    not _text(rows.get(r_p, {}).get(pop_col, {}))
                    or _compact(_text(rows.get(r_p, {}).get(pop_col, {}))) in {"（", "）", "(", ")"}
                    or _is_unit_token(_text(rows.get(r_p, {}).get(pop_col, {})), allow_measure=False)
                )
                for r_p in range(1, sub_row)
            )
            for ecol in empty_cols
        ):
            result = {col: value for col, value in result.items() if value != field or col == pop_col}
    return result


def _table_header(rows, columns, key_field):
    mapping, header_end = {}, None
    committed_mapping, committed_end = {}, None
    # 按列追踪挂起中的未映射修饰/单位/拆字状态集合：{col: set[(kind, text)]}，kind in {"unit", "group", "split"}
    pending_unmapped_cols = {}
    for end in sorted(rows)[:8]:
        row = rows[end]
        row_non_empty = any(_text(c) for c in row.values())
        row_flags = _row_decorative_flags(row, mapping, columns)
        if header_end is not None:
            # 最多允许跨过 1 个空行；若间隔超过 1 个空行或中间存在未被消费的非空行，立即停止扩张
            if end - header_end > 2:
                break
            if any(any(_text(c) for c in rows.get(r, {}).values()) for r in sorted(rows) if header_end < r < end):
                break
            if _is_data_row(row, mapping, rows, header_end, end, columns, row_flags=row_flags):
                break

        candidate = {}
        candidate_from_split = set()
        for col in range(1, columns + 1):
            window_raws = [_text(rows.get(r, {}).get(col, {})) for r in sorted(rows) if max(1, end - 2) <= r <= end]
            labels = [(_header(raw), raw) for raw in window_raws]
            non_empty_labels = [lbl for lbl, _ in labels if lbl]
            two_line_join = "".join(non_empty_labels[-2:]) if len(non_empty_labels) >= 2 else ""
            all_join = "".join(non_empty_labels)
            # 若前序表头中该未映射列已有独立列名或非法备注（如“备注”“说明”“费用（注：不含规费）”），
            # 无论 header_end 是否已确立，仅允许跨行拆字拼合，禁止单靠后续行覆盖篡改字段映射
            restrict_to_join = False
            if end > 1 and col not in mapping:
                prev_texts = [_text(rows.get(r, {}).get(col, {})) for r in sorted(rows) if 1 <= r < end]
                has_pending_split = any(k == "split" for k, _ in pending_unmapped_cols.get(col, ()))
                restrict_to_join = any(prev_texts) and not _unmapped_prev_allows_continuation(prev_texts, has_pending_split)
            variant_items = (
                [(all_join, "")]
                if restrict_to_join
                else [*reversed(labels), *([(two_line_join, "")] if two_line_join else []), (all_join, "")]
            )
            matched_label, field = next(
                (
                    (lbl, f)
                    for lbl, raw in variant_items
                    if lbl
                    for f in (_match_alias_for_label(lbl, raw),)
                    if f is not None
                ),
                (None, None),
            )
            if field:
                candidate[col] = field
                cur_label = labels[-1][0] if labels else ""
                if len(non_empty_labels) >= 2 and (
                    matched_label != cur_label
                    or (
                        cur_label != non_empty_labels[-2]
                        and (_match_alias_for_label(two_line_join) == field or _match_alias_for_label(all_join) == field)
                    )
                ):
                    candidate_from_split.add(col)

        merged = _deduplicate_spanning_amount({**mapping, **candidate}, rows, end)
        is_dec_row = header_end is not None and _is_header_decorative_row(row, mapping, columns, row_flags=row_flags)
        mapped_non_empty = any(col in mapping and _text(cell) for col, cell in row.items())
        allow_paren_colnum, allow_placeholder, valid_formula_cols = row_flags
        newly_mapped = [col for col in candidate if col not in mapping]
        mapped_non_dec_cols = [
            col
            for col in row
            if col in mapping
            and _text(row.get(col, {}))
            and not _is_decorative_token(
                _text(row.get(col, {})),
                mapping[col],
                allow_paren_colnum=allow_paren_colnum,
                allow_placeholder=allow_placeholder,
                allow_paren_formula=(col in valid_formula_cols),
            )
        ]
        has_mapped_split = header_end is not None and any(col in candidate_from_split for col in mapped_non_dec_cols)
        has_mapped_placeholder = any(
            col in mapping and _compact(_text(cell)) in _HEADER_PLACEHOLDERS
            for col, cell in row.items()
        )
        has_mapped_sub_or_repeat = (
            header_end is not None
            and not has_mapped_placeholder
            and bool(mapped_non_dec_cols)
            and (any(mapping[col] in _NUMERIC for col in mapped_non_dec_cols) or len(mapped_non_dec_cols) >= 2)
        )

        # 按列累加未映射挂起状态集合，绝不让后续 group 覆写擦除曾出现的 unit 或 split
        next_pending = {k: set(v) for k, v in pending_unmapped_cols.items()}
        split_validation_ok = True
        if header_end is not None:
            for col, cell in row.items():
                text = _text(cell)
                if not text or col in merged or col in mapping:
                    continue
                is_dec_tok = _is_decorative_token(
                    text,
                    field=None,
                    allow_paren_colnum=allow_paren_colnum,
                    allow_placeholder=allow_placeholder,
                    allow_paren_formula=(col in valid_formula_cols),
                )
                prev_texts = [
                    _text(rows.get(r, {}).get(col, {}))
                    for r in sorted(rows)
                    if 1 <= r <= header_end and _text(rows.get(r, {}).get(col, {}))
                ]
                is_spanning_amount_col = (
                    bool(prev_texts)
                    and all(_is_spanning_amount_fragment(t, allow_placeholder=False) for t in prev_texts)
                    and any(_is_generic_amount_parent_text(t) for t in prev_texts)
                )
                if is_dec_row and mapped_non_empty:
                    if _is_unit_token(text, allow_measure=True) and not is_spanning_amount_col:
                        next_pending.setdefault(col, set()).add(("unit", text))
                elif is_dec_tok:
                    if not ((newly_mapped or has_mapped_split or has_mapped_sub_or_repeat) and (not prev_texts or is_spanning_amount_col)):
                        next_pending.setdefault(col, set()).add(("unit", text))
                elif _is_parent_header_modifier(text):
                    next_pending.setdefault(col, set()).add(("group", text))
                elif _is_split_alias_prefix(text):
                    next_pending.setdefault(col, set()).add(("split", _header(text)))

        for col in newly_mapped:
            pending_entries = next_pending.get(col)
            if pending_entries and any(k == "split" for k, _ in pending_entries):
                if col in candidate_from_split:
                    next_pending.pop(col, None)
                else:
                    split_validation_ok = False
            else:
                next_pending.pop(col, None)
            # 仅当左侧紧邻未映射列的挂起类型纯粹为 "group" 且中间无已映射列阻隔时才解除挂起
            for pcol in list(next_pending):
                if pcol < col and {k for k, _ in next_pending[pcol]} == {"group"} and not any(mid in merged for mid in range(pcol + 1, col)):
                    next_pending.pop(pcol, None)

        has_unmapped_non_empty = any(col not in merged and _text(cell) for col, cell in row.items())
        unmapped_ok = (
            header_end is None
            or not has_unmapped_non_empty
            or _is_valid_header_continuation_row(rows, header_end, row, merged, columns, pending_unmapped_cols)
        )

        if (
            key_field in merged.values()
            and any(f in _NUMERIC for f in merged.values())
            and (header_end is None or set(mapping.values()) <= set(merged.values()))
            and split_validation_ok
            and unmapped_ok
            and (
                header_end is None
                or len(set(merged.values())) > len(set(mapping.values()))
                or len(merged) > len(mapping)
                or has_mapped_split
                or has_mapped_sub_or_repeat
            )
        ):
            mapping, header_end = merged, end
            pending_unmapped_cols = next_pending
            if not pending_unmapped_cols:
                committed_mapping, committed_end = merged, end
        elif is_dec_row:
            header_end = end
            pending_unmapped_cols = next_pending
            if not pending_unmapped_cols and mapped_non_empty:
                committed_end = end
        elif (
            header_end is not None
            and set(mapping.values()) <= set(merged.values())
            and _is_valid_header_continuation_row(rows, header_end, row, mapping, columns, pending_unmapped_cols, row_flags=row_flags)
        ):
            header_end = end
            pending_unmapped_cols = next_pending
        elif header_end is not None and row_non_empty:
            # 遇到未能被表头消费的非空行，立即停止扩张，防止后续行越过该行吞并数据
            break
    if committed_end is not None:
        committed_mapping = _deduplicate_spanning_amount(committed_mapping, rows, committed_end)
    return committed_mapping, committed_end


def _is_repeated_body_header_row(row, mapping, key_field, columns, header_col_specs=None):
    # 极简铁律：表体重复表头行仅允许严格匹配当前表原表头 1..header_end 各列实际出现过的标签、单位或精确装饰原文，
    # 彻底禁止脱离原表头的父修饰放行（is_mod）或无原表头校验的装饰放行（is_dec），宁可保守留入表体触发 pending。
    if not header_col_specs:
        return False
    key_col = next((col for col, field in mapping.items() if field == key_field), None)
    if key_col is None:
        return False
    key_text = _compact(_text(row.get(key_col, {})))
    key_spec = header_col_specs.get(key_col)
    key_cleaned = _header(key_text)
    if (
        not key_text
        or not key_spec
        or key_cleaned not in _ALIASES[key_field]
        or key_cleaned not in key_spec["labels"]
    ):
        return False
    seq_col = next((col for col, field in mapping.items() if field == "sequence"), None)
    if seq_col is not None:
        seq_text = _compact(_text(row.get(seq_col, {})))
        if seq_text and re.fullmatch(r"\d+(?:\.\d+)*", seq_text):
            return False
    other_mapped_header_count = 0
    for col, cell in row.items():
        text = _text(cell)
        if not text:
            continue
        field = mapping.get(col)
        if field in _NUMERIC:
            try:
                if _number(text) is not None:
                    return False
            except ValueError:
                pass
        col_spec = header_col_specs.get(col)
        if not col_spec:
            return False
        compact_text = _compact(text)
        cleaned = _header(text)
        # 1. 占位符、纯括号列号、括号公式或去括号后为空的纯标点（如“((()))”“（”）：
        #    仅允许与原表头该列实际出现过的归一化原文完全一致，严禁无条件放行
        if (
            compact_text in _HEADER_PLACEHOLDERS
            or _extract_paren_colnum(compact_text) is not None
            or _is_paren_formula(compact_text)
            or not cleaned
        ):
            if _norm_paren_formula(compact_text) not in col_spec["exact"]:
                return False
            continue
        # 2. 纯单位 token（如“元”“万元”“元/m2”）：
        #    仅允许与原表头该列确立的独立单位（或原表头该列标签）一致，严禁放行新单位或冲突单位
        if _is_unit_token(compact_text, allow_measure=True):
            unwrapped = re.sub(r"^[（(](.+)[）)]$", r"\1", compact_text)
            if _norm_unit(unwrapped) not in col_spec["units"] and cleaned not in col_spec["labels"]:
                return False
            continue
        # 3. 普通表头文字（含已映射与未映射列）：必须严格存在于原表头该列实际标签集合中
        if cleaned not in col_spec["labels"]:
            return False
        if field is not None and col != key_col and _match_alias_for_label(cleaned, text) == field:
            other_mapped_header_count += 1
    return other_mapped_header_count >= 1


def _is_continuation_header_compatible(first_sig, current_sig):
    # 续表表头一致性校验：仅阻断显式冲突或续表新增首页不存在的单位/口径/公式，
    # 允许真实多页续表省略列号行、省略已确立单位行或取消跨列父表头直接写子列名。
    for field in set(first_sig["field_units"]) & set(current_sig["field_units"]):
        u2 = current_sig["field_units"][field]
        if u2 and u2 != first_sig["field_units"][field]:
            return False
    for field in set(first_sig["field_modifiers"]) & set(current_sig["field_modifiers"]):
        m2 = current_sig["field_modifiers"][field]
        if m2 and m2 != first_sig["field_modifiers"][field]:
            return False
    if current_sig["unmapped"] and current_sig["unmapped"] != first_sig["unmapped"]:
        return False
    if current_sig["colnums"] and current_sig["colnums"] != first_sig["colnums"]:
        return False
    if current_sig["formulas"] and current_sig["formulas"] != first_sig["formulas"]:
        return False
    return True


def _extract_with_header_sig(book, report, code):
    names = report.get("member_sheets") or ([report["sheet_name"]] if report.get("sheet_name") else [])
    by_name = {sheet["name"]: sheet for sheet in book.get("sheets", [])}
    records, issues, fields = [], [], set()
    first_fields = None
    first_header_sig = None
    key_field = "code" if code == "E.1" else "name"
    if not names:
        return [], set(), ["没有可提取的报表物理表格"], None
    for page in book.get("pages", []):
        if page.get("number", page.get("page")) in report.get("pages", []):
            if page.get("status") in ("unavailable", "failed") or page.get("structure_unconfirmed"):
                issues.append("关联页面提取失败或结构尚未确认")
    for name in dict.fromkeys(names):
        sheet = by_name.get(name)
        if not sheet or sheet.get("structure") != "table":
            issues.append(f"{name}：表格结构不可用")
            continue
        if sheet.get("structure_unconfirmed") or sheet.get("structure_confirmed") is False:
            issues.append(f"{name}：表格结构尚未确认")
        rows = {}
        for cell in sheet.get("cells", {}).values():
            rows.setdefault(cell["row"], {})[cell["column"]] = cell
        max_col = sheet.get("max_column", 0)
        mapping, header_end = _table_header(rows, max_col, key_field)
        if header_end is None:
            issues.append(f"{name}：未识别到业务键和数值表头")
            continue
        if any(cell.get("pdf_source", {}).get("method") == "ocr" and not cell.get("reviewed")
               for r, cells in rows.items() if r <= header_end for cell in cells.values()):
            issues.append(f"{name}：OCR 表头尚未核验")
        counts = Counter(mapping.values())
        if any(count > 1 for count in counts.values()):
            issues.append(f"{name}：多个表头映射到相同业务字段，需确认列含义")
            continue
        required = {"code", "name", "description", "unit", "quantity", "unit_price", "amount"} if code == "E.1" else {"name", "amount"}
        if not required <= set(mapping.values()):
            issues.append(f"{name}：缺少必要业务字段：" + "、".join(_LABELS[f] for f in sorted(required - set(mapping.values()))))
        current_sig, consumed_unmapped_cols = _analyze_header_signature(rows, mapping, header_end, max_col)
        header_row_flags = {
            r: _row_decorative_flags(rows.get(r, {}), mapping, max_col)
            for r in rows
            if r <= header_end
        }
        for col in range(1, max_col + 1):
            if col in mapping or col in consumed_unmapped_cols:
                continue
            body_present = any(_text(cells.get(col, {})) for r, cells in rows.items() if r > header_end)
            decorative = {"序号", "其中", "元", "定额"}
            has_unmapped_header = False
            has_any_header_text = False
            for r in rows:
                if r > header_end:
                    continue
                raw = _text(rows.get(r, {}).get(col, {}))
                if not _header(raw):
                    continue
                has_any_header_text = True
                allow_colnum, allow_ph, valid_fcols = header_row_flags[r]
                is_dec = _is_decorative_token(
                    raw,
                    allow_paren_colnum=allow_colnum,
                    allow_placeholder=allow_ph,
                    allow_paren_formula=(col in valid_fcols),
                )
                if (_header(raw) not in decorative and not _is_generic_amount_parent_text(raw) and not is_dec) or body_present:
                    has_unmapped_header = True
                    break
            if has_unmapped_header or (body_present and not has_any_header_text):
                issues.append(f"{name}：存在未映射表头，第 {col} 列未比较")
        for col, field in sorted(mapping.items()):
            if not _is_valid_mapped_header_column_cells(rows, col, field, header_end, header_row_flags):
                issues.append(f"{name}：第 {col} 列前序表头含不可识别备注或未知限定文本，需人工确认")
        current_fields = set(mapping.values())
        if first_fields is not None and current_fields != first_fields:
            issues.append(f"{name}：续表业务字段覆盖不同，需确认列映射")
        first_fields = current_fields if first_fields is None else first_fields
        if current_sig.get("spanning_conflict"):
            issues.append(f"{name}：跨列父表头与子列单位或口径冲突，需人工确认")
        if current_sig.get("unit_conflict"):
            issues.append(f"{name}：表头同列存在相互冲突的单位，需人工确认")
        if current_sig.get("modifier_conflict"):
            issues.append(f"{name}：表头存在相互冲突的口径修饰（含税/不含税），需人工确认")
        if current_sig.get("formula_conflict"):
            issues.append(f"{name}：多行表头公式列号无法唯一对应业务列，需人工确认")
        if first_header_sig is not None and not _is_continuation_header_compatible(first_header_sig, current_sig):
            issues.append(f"{name}：续表表头单位、口径修饰或列号结构不同，需确认")
        first_header_sig = current_sig if first_header_sig is None else first_header_sig
        fields.update(current_fields - {"sequence"})
        # 按物理列收集当前表原表头 1..header_end 实际出现过的标签、独立单位与精确装饰原文，
        # 将括号列号/公式/占位符/纯标点与普通文字标签严格隔离，防止剥括号数字污染标签集合。
        header_col_specs = {}
        for col_idx in range(1, max_col + 1):
            col_lbls, col_units, col_exact, joined_parts = set(), set(), set(), []
            for r_idx in sorted(rows):
                if not (1 <= r_idx <= header_end):
                    continue
                raw_text = _text(rows.get(r_idx, {}).get(col_idx, {}))
                if not raw_text:
                    continue
                compact_text = _compact(raw_text)
                if (
                    compact_text in _HEADER_PLACEHOLDERS
                    or _extract_paren_colnum(compact_text) is not None
                    or _is_paren_formula(compact_text)
                    or not _header(raw_text)
                ):
                    col_exact.add(_norm_paren_formula(compact_text))
                    continue
                lbl = _header(raw_text)
                col_lbls.add(lbl)
                joined_parts.append(lbl)
                if _is_unit_token(compact_text, allow_measure=True):
                    unwrapped = re.sub(r"^[（(](.+)[）)]$", r"\1", compact_text)
                    col_units.add(_norm_unit(unwrapped))
            if joined_parts:
                col_lbls.add("".join(joined_parts))
            col_field = mapping.get(col_idx)
            if col_field:
                for lbl in list(col_lbls):
                    for unit in _SORTED_UNITS:
                        if lbl.endswith(unit) and _match_alias_for_label(lbl[: -len(unit)]) == col_field:
                            col_lbls.add(lbl[: -len(unit)])
            if col_lbls or col_units or col_exact:
                header_col_specs[col_idx] = {"labels": col_lbls, "units": col_units, "exact": col_exact}
        for row_number, cells in sorted(rows.items()):
            if row_number <= header_end:
                continue
            values = {field: _text(cells.get(col, {})) for col, field in mapping.items()}
            if not any(values.values()):
                continue
            key = _compact(values.get(key_field, ""))
            if _is_repeated_body_header_row(cells, mapping, key_field, max_col, header_col_specs):
                continue
            name_value = _compact(values.get("name", ""))
            if any(value.startswith(("注：", "注:", "说明：", "说明:")) for value in values.values()):
                continue
            # E.1 sections and totals are not bill items; D tables retain their totals.
            if code == "E.1" and (name_value in ("合计", "总计", "小计", "本页小计", "分部小计") or
                                    key in ("合计", "总计", "小计", "本页小计", "分部小计")):
                continue
            if code == "E.1" and re.fullmatch(r"\d{3,4}", key) and not values.get("sequence") and not any(
                    values.get(field) for field in ("description", "unit", "quantity", "unit_price")):
                continue
            sources = {field: _source(sheet, cells[col]) for col, field in mapping.items() if col in cells}
            if code != "E.1" and not key and re.match(r"^(?:合计|总计|工程造价)", _compact(values.get("sequence", ""))):
                values["name"] = values["sequence"]
                key = _compact(values["name"])
                sources["name"] = sources.get("sequence")
            if code == "D.3" and re.fullmatch(r"\d+(?:\.\d+)+", _compact(values.get("sequence", ""))):
                key = _compact(values["sequence"]) + " · " + key
            if not key:
                issues.append(f"{name} 第 {row_number} 行：业务键为空，不能确认对应关系")
            for col, field in mapping.items():
                cell = cells.get(col, {})
                if cell.get("pdf_source", {}).get("method") == "ocr" and not cell.get("reviewed"):
                    issues.append(f"{name} 第 {row_number} 行：OCR 内容尚未核验")
                if field in _NUMERIC and values[field]:
                    try:
                        _number(values[field])
                    except ValueError:
                        issues.append(f"{name} 第 {row_number} 行：{_LABELS[field]}不是可确认的数值")
            records.append({"key": key, "values": values, "sources": sources})
    duplicates = [key for key, count in Counter(record["key"] for record in records).items() if key and count > 1]
    if duplicates:
        issues.append("业务键重复，需确认组合键：" + "、".join(duplicates))
    if not records:
        issues.append("未提取到业务数据行，不能据此判定报表一致")
    return records, fields, list(dict.fromkeys(issues)), first_header_sig


def _extract(book, report, code):
    return _extract_with_header_sig(book, report, code)[:3]


def _header_signature_issues(old_sig, new_sig):
    # 比对两侧表头的单位、口径修饰词、未映射分组修饰、多行括号列号及公式结构差异，绝不放行隐性万倍单位、口径或公式差
    if not old_sig or not new_sig:
        return []
    issues = []
    for field in sorted(set(old_sig["field_units"]) & set(new_sig["field_units"])):
        u1, u2 = old_sig["field_units"][field], new_sig["field_units"][field]
        if u1 != u2:
            issues.append(
                f"表头单位不一致（{_LABELS.get(field, field)}：旧版 {'/'.join(sorted(u1)) or '无'} vs 新版 {'/'.join(sorted(u2)) or '无'}），需人工确认"
            )
    for field in sorted(set(old_sig["field_modifiers"]) & set(new_sig["field_modifiers"])):
        m1, m2 = old_sig["field_modifiers"][field], new_sig["field_modifiers"][field]
        if m1 != m2:
            issues.append(
                f"表头口径修饰不一致（{_LABELS.get(field, field)}：旧版 {'/'.join(sorted(m1)) or '无'} vs 新版 {'/'.join(sorted(m2)) or '无'}），需人工确认"
            )
    if old_sig.get("spanning_conflict") or new_sig.get("spanning_conflict"):
        issues.append("跨列父表头与子列单位或口径冲突，需人工确认")
    if old_sig.get("unit_conflict") or new_sig.get("unit_conflict"):
        issues.append("表头同列存在相互冲突的单位，需人工确认")
    if old_sig.get("modifier_conflict") or new_sig.get("modifier_conflict"):
        issues.append("表头存在相互冲突的口径修饰（含税/不含税），需人工确认")
    if old_sig.get("formula_conflict") or new_sig.get("formula_conflict"):
        issues.append("多行表头公式列号无法唯一对应业务列，需人工确认")
    if old_sig["unmapped"] != new_sig["unmapped"]:
        issues.append("表头未映射分组单位或修饰不一致，需人工确认")
    if old_sig["colnums"] != new_sig["colnums"]:
        issues.append("多行表头列号结构不一致，需人工确认")
    if old_sig.get("formulas") != new_sig["formulas"]:
        issues.append("多行表头列号公式不一致，需人工确认")
    return issues


def compare_cost_reports(old_book, new_book, inventory):
    """Return semantic diffs and per-report coverage, without mutating evidence.

    Only confirmed same-type report pairs are compared. Extraction uncertainty
    produces an issue and pending coverage, never an unchanged conclusion.
    """
    diffs, summaries = [], []
    for entry in inventory.get("reports", []):
        left, right = entry.get("old"), entry.get("new")
        code = _code(left or right or {})
        if code not in _SUPPORTED:
            continue
        summary = {"report_id": entry["id"], "type_code": code, "status": "pending", "compared_rows": 0,
                   "compared_fields": 0, "issues": [], "changes": 0, "issue_count": 0}
        summaries.append(summary)

        def emit(kind, key="", field="", before=None, after=None, delta=None, message="", severity="change"):
            old_source = (before or {}).get("sources", {}).get(field)
            new_source = (after or {}).get("sources", {}).get(field)
            if severity == "issue":
                old_source = {**left["source"], "location_precision": "corresponding_region"} if left and left.get("source") else None
                new_source = {**right["source"], "location_precision": "corresponding_region"} if right and right.get("source") else None
            kind = {"value_changed": "business_value_changed", "row_added": "business_row_added",
                    "row_removed": "business_row_removed", "cost_review": "business_issue"}[kind]
            diffs.append({"id": f"cost-diff-{len(diffs) + 1:07d}", "kind": kind, "table_id": entry["id"],
                          "report_id": entry["id"], "table_name": entry.get("title", "造价报表"), "row_key": key,
                          "field": _LABELS.get(field, field or "业务数据"), "old_value": (before or {}).get("values", {}).get(field),
                          "new_value": (after or {}).get("values", {}).get(field), "delta": delta,
                          "old_source": old_source, "new_source": new_source, "message": message,
                          "severity": severity, "category": "business", "layer": "business"})

        if not left or not right or entry.get("confirmation") != "confirmed" or _code(right) != code:
            summary["issues"] = ["报表未确认成对或类型不一致，业务数据未比较"]
        elif entry.get("match_method") in ("sequence", "remaining"):
            summary["issues"] = ["仅按剩余数量或顺序配对，工程层级身份尚未确认；请人工确认业务报表"]
        elif entry.get("match_method") != "manual" and any(report.get("duplicate") for report in (left, right)):
            summary["issues"] = ["同名工程报表存在重复实例，需确认父级工程和实例对应关系"]
        elif entry.get("match_method") != "manual" and any(report.get("cost_identity_issue") for report in (left, right)):
            summary["issues"] = [report["cost_identity_issue"] for report in (left, right) if report.get("cost_identity_issue")]
        elif code == "D.2" and ("投标报价" in left.get("title", "")) != ("投标报价" in right.get("title", "")):
            summary["issues"] = ["D.2 限价汇总与投标报价属于不同业务报表，不能直接按同类比较"]
        elif entry.get("blocking_reasons"):
            summary["issues"] = list(entry["blocking_reasons"])
        else:
            old_rows, old_fields, old_issues, old_sig = _extract_with_header_sig(old_book, left, code)
            new_rows, new_fields, new_issues, new_sig = _extract_with_header_sig(new_book, right, code)
            summary["issues"] = [*("旧版：" + i for i in old_issues), *("新版：" + i for i in new_issues)]
            if old_fields != new_fields:
                summary["issues"].append("两侧业务字段覆盖不同，需确认字段映射")
            else:
                summary["issues"].extend(_header_signature_issues(old_sig, new_sig))
            if not summary["issues"]:
                old_map = {row["key"]: row for row in old_rows}
                new_map = {row["key"]: row for row in new_rows}
                start = len(diffs)
                key_field = "code" if code == "E.1" else "name"
                for key in dict.fromkeys([*old_map, *new_map]):
                    before, after = old_map.get(key), new_map.get(key)
                    if not before or not after:
                        emit("row_added" if after else "row_removed", key, key_field, before, after,
                             message="已确认报表内按业务键对应，该条目仅存在于一侧。")
                        continue
                    summary["compared_rows"] += 1
                    for field in sorted(old_fields - {key_field}):
                        a, b = before["values"].get(field, ""), after["values"].get(field, "")
                        delta = None
                        summary["compared_fields"] += 1
                        if field in _NUMERIC:
                            na, nb = _number(a), _number(b)
                            equal = na == nb
                            if na is not None and nb is not None:
                                with localcontext() as context:
                                    context.prec = max(28, len(a) + len(b) + 4)
                                    delta = format(nb - na, "f")
                        else:
                            equal = _compact(a) == _compact(b)
                        if not equal:
                            emit("value_changed", key, field, before, after, delta,
                                 "按业务字段比较；差额为新版减旧版，空白与零不同。")
                summary["changes"] = len(diffs) - start
                summary["status"] = "changed" if summary["changes"] else "unchanged"
        if summary["issues"]:
            summary["issue_count"] = len(summary["issues"])
            emit("cost_review", message="；".join(summary["issues"]), severity="issue")
    return {"diffs": diffs, "reports": summaries}

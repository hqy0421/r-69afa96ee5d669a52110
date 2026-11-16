"""
pyexcel.mrp
~~~~~~~~~~~

Multi-level material requirements planning (MRP) built on top of a
:class:`pyexcel.Book`.

The input book must contain four sheets (Chinese sheet/column names by
default, all overridable):

* **物料** (material master): ``物料编码``、``物料名称``(可选)、
  ``批量``(可选，缺省为 1)
* **BOM**: ``父件编码``、``子件编码``、``用量``
* **订单需求** (independent demand): ``订单号``(可选)、
  ``需求物料编码``、``需求数量``
* **库存**: ``物料编码``、``库存数量``

The planner explodes the BOM level by level.  Gross requirements of a
shared component are merged before its stock is deducted exactly once,
planned production/purchase quantities are rounded up to each material's
lot size, and lower-level requirements follow the *planned* (rounded up)
output of the parent rather than the raw net demand.

All quantities stay integer-exact.  Invalid references, duplicate data
and circular BOMs are collected and raised together as a single
:class:`MRPError`; a partial plan is never returned.  The source book is
only read and is never modified.

example::

    >>> import pyexcel as pe
    >>> from pyexcel.mrp import build_material_plan
    >>> book = pe.get_book(bookdict={
    ...     "物料": [["物料编码", "物料名称", "批量"],
    ...             ["A", "成品", 1],
    ...             ["B", "组件", 2],
    ...             ["C", "螺丝", 100]],
    ...     "BOM": [["父件编码", "子件编码", "用量"],
    ...            ["A", "B", 1],
    ...            ["B", "C", 2]],
    ...     "订单需求": [["订单号", "需求物料编码", "需求数量"],
    ...                ["SO1", "A", 5]],
    ...     "库存": [["物料编码", "库存数量"],
    ...            ["C", 50]],
    ... })
    >>> plan = build_material_plan(book)
    >>> plan.sheet_names()
    ['备料计划', '父子需求明细']
    >>> plan["备料计划"].to_array()  # doctest: +NORMALIZE_WHITESPACE
    [['物料编码', '物料名称', '层级', '毛需求', '库存', '净需求',
      '批量', '生产或采购量', '预计结存'],
     ['A', '成品', 0, 5, 0, 5, 1, 5, 0],
     ['B', '组件', 1, 5, 0, 5, 2, 6, 1],
     ['C', '螺丝', 2, 12, 50, 0, 100, 0, 38]]
    >>> plan["父子需求明细"].to_array()  # doctest: +NORMALIZE_WHITESPACE
    [['需求类型', '订单号/来源', '父件编码', '子件/需求物料编码',
      '单位用量', '父件投产量', '毛需求数量'],
     ['独立需求', 'SO1', '', 'A', '', '', 5],
     ['BOM展开', '', 'A', 'B', 1, 5, 5],
     ['BOM展开', '', 'B', 'C', 2, 6, 12]]

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details.
"""

from collections import OrderedDict, defaultdict

from pyexcel import Book


# Default sheet names --------------------------------------------------------
DEFAULT_SHEET_NAMES = {
    "material": "物料",
    "bom": "BOM",
    "demand": "订单需求",
    "stock": "库存",
}

# Default candidate column names per logical field.  User supplied names
# are prepended to these candidates.
DEFAULT_COLUMN_NAMES = {
    "material_code": ["物料编码", "物料编号", "料号"],
    "material_name": ["物料名称", "名称"],
    "lot_size": ["批量", "订货批量", "采购批量"],
    "bom_parent": ["父件编码", "父件", "父物料编码"],
    "bom_child": ["子件编码", "子件", "子物料编码"],
    "bom_usage": ["用量", "单位用量", "需求用量"],
    "demand_order": ["订单号", "订单编号", "销售订单"],
    "demand_material": ["需求物料编码", "物料编码", "料号"],
    "demand_qty": ["需求数量", "数量", "毛需求数量"],
    "stock_material": ["物料编码", "物料编号", "料号"],
    "stock_qty": ["库存数量", "库存量", "现有库存"],
}

PLAN_SHEET = "备料计划"
DETAIL_SHEET = "父子需求明细"

PLAN_HEADER = [
    "物料编码", "物料名称", "层级", "毛需求", "库存", "净需求",
    "批量", "生产或采购量", "预计结存",
]
DETAIL_HEADER = [
    "需求类型", "订单号/来源", "父件编码", "子件/需求物料编码",
    "单位用量", "父件投产量", "毛需求数量",
]


class MRPError(Exception):
    """Raised when validation fails.

    The message lists every problem found in the batch; no plan book is
    produced when this exception is raised.
    """


def build_material_plan(
    book,
    sheet_names=None,
    columns=None,
    plan_sheet_name=PLAN_SHEET,
    detail_sheet_name=DETAIL_SHEET,
):
    """Generate a multi-level material preparation plan.

    :param book: a :class:`pyexcel.Book` holding the four source sheets.
    :param dict sheet_names: optional mapping of logical sheet keys
        (``material``/``bom``/``demand``/``stock``) to the actual sheet
        names in the book.
    :param dict columns: optional mapping of logical column keys to a
        column name or a list of candidate names; they take precedence
        over the built-in Chinese defaults.
    :param str plan_sheet_name: name of the plan sheet in the output.
    :param str detail_sheet_name: name of the parent/child detail sheet
        in the output.
    :returns: a brand new in-memory :class:`pyexcel.Book` containing the
        plan sheet and the parent/child demand detail sheet.  The source
        book is never modified.
    :raises MRPError: if any invalid reference, duplicate datum or
        circular BOM is found; all problems are reported at once.
    """
    if not isinstance(book, Book):
        raise MRPError(
            "build_material_plan 需要 pyexcel.Book 作为输入，实际收到: %s"
            % type(book).__name__
        )

    sheet_keys = dict(DEFAULT_SHEET_NAMES)
    if sheet_names:
        for key, value in sheet_names.items():
            if key not in sheet_keys:
                raise MRPError("未知的 sheet 逻辑名: %s" % key)
            sheet_keys[key] = value

    column_names = {}
    for key, candidates in DEFAULT_COLUMN_NAMES.items():
        column_names[key] = list(candidates)
    if columns:
        for key, value in columns.items():
            if key not in column_names:
                raise MRPError("未知的列逻辑名: %s" % key)
            extras = value if isinstance(value, (list, tuple)) else [value]
            column_names[key] = list(extras) + column_names[key]

    errors = []
    arrays = _resolve_sheets(book, sheet_keys, errors)
    # Header indices: key -> column index (missing -> None)
    header_maps = {}
    for logical, specs in (
        (
            "material",
            (
                ("material_code", True),
                ("material_name", False),
                ("lot_size", False),
            ),
        ),
        (
            "bom",
            (
                ("bom_parent", True),
                ("bom_child", True),
                ("bom_usage", True),
            ),
        ),
        (
            "demand",
            (
                ("demand_order", False),
                ("demand_material", True),
                ("demand_qty", True),
            ),
        ),
        (
            "stock",
            (
                ("stock_material", True),
                ("stock_qty", True),
            ),
        ),
    ):
        label = "Sheet「%s」" % sheet_keys[logical]
        if arrays.get(logical) is None or not arrays[logical]:
            if arrays.get(logical) is not None:
                errors.append("%s为空，缺少表头行" % label)
            header_maps[logical] = None
            continue
        header_maps[logical] = _parse_header(
            arrays[logical], specs, column_names, label, errors
        )

    materials = OrderedDict()
    stock = {}
    demand_rows = []
    bom_rows = []

    if header_maps.get("material") is not None \
            and header_maps["material"]["material_code"] is not None:
        _parse_materials(
            arrays["material"], header_maps["material"], materials, errors
        )
    if header_maps.get("stock") is not None \
            and header_maps["stock"]["stock_material"] is not None:
        _parse_stock(
            arrays["stock"], header_maps["stock"], stock, errors
        )
    if header_maps.get("demand") is not None \
            and header_maps["demand"]["demand_material"] is not None:
        _parse_demand(
            arrays["demand"], header_maps["demand"], demand_rows, errors
        )
    if header_maps.get("bom") is not None \
            and header_maps["bom"]["bom_parent"] is not None:
        _parse_bom(arrays["bom"], header_maps["bom"], bom_rows, errors)

    _validate_references(
        materials, stock, demand_rows, bom_rows, sheet_keys, errors
    )
    cycle = _detect_cycle(materials, bom_rows)
    if cycle is not None:
        errors.append(
            "BOM 存在循环引用: %s" % " -> ".join(cycle)
        )

    if errors:
        raise MRPError(
            "备料计划生成失败，发现 %d 个数据问题:\n%s"
            % (len(errors), "\n".join("%d. %s" % (i, e)
                                      for i, e in enumerate(errors, 1)))
        )

    return _calculate(
        materials, stock, demand_rows, bom_rows,
        plan_sheet_name, detail_sheet_name,
    )


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------

def _resolve_sheets(book, sheet_keys, errors):
    arrays = {}
    available = "、".join(book.sheet_names()) or "(空 Book)"
    for logical, sheet_name in sheet_keys.items():
        if sheet_name not in book.sheet_names():
            errors.append(
                "缺少 Sheet「%s」；当前 Book 包含: %s"
                % (sheet_name, available)
            )
            arrays[logical] = None
        else:
            arrays[logical] = book.sheet_by_name(sheet_name).to_array()
    return arrays


def _parse_header(rows, specs, column_names, label, errors):
    if not rows:
        errors.append("%s为空，缺少表头行" % label)
        return None
    header = rows[0]
    normalized = [
        "" if _is_blank(cell) else str(cell).strip() for cell in header
    ]
    positions = {}
    for index, name in enumerate(normalized):
        if name == "":
            continue
        if name in positions:
            errors.append(
                "%s表头列名重复: 「%s」同时出现在第%d列和第%d列"
                % (label, name, positions[name] + 1, index + 1)
            )
        else:
            positions[name] = index

    index_map = {}
    for logical_key, required in specs:
        found = None
        for candidate in column_names[logical_key]:
            if candidate in positions:
                found = positions[candidate]
                break
        if found is None:
            if required:
                errors.append(
                    "%s缺少必需列「%s」；现有列: %s"
                    % (
                        label,
                        "/".join(column_names[logical_key]),
                        "、".join(n for n in normalized if n) or "(空)",
                    )
                )
            index_map[logical_key] = None
        else:
            index_map[logical_key] = found
    return index_map


def _parse_materials(rows, index_map, materials, errors):
    label = "Sheet「物料」"
    seen = set()
    for row_no, row in enumerate(rows[1:], start=2):
        if _is_empty_row(row):
            continue
        code = _norm_code(_cell(row, index_map["material_code"]))
        if code == "":
            errors.append("%s第%d行物料编码不能为空" % (label, row_no))
            continue
        if code in seen:
            errors.append(
                "%s第%d行物料编码「%s」重复" % (label, row_no, code)
            )
            continue
        seen.add(code)
        name_idx = index_map.get("material_name")
        name = ""
        if name_idx is not None and name_idx < len(row):
            name = "" if _is_blank(row[name_idx]) else str(
                row[name_idx]
            ).strip()
        lot = 1
        lot_idx = index_map.get("lot_size")
        if lot_idx is not None and lot_idx < len(row) \
                and not _is_blank(row[lot_idx]):
            lot = _parse_int(
                row[lot_idx],
                errors,
                "%s第%d行批量" % (label, row_no),
                positive=True,
            )
            if lot is None:
                lot = 1
        materials[code] = {"name": name, "lot": lot}


def _parse_stock(rows, index_map, stock, errors):
    label = "Sheet「库存」"
    seen = set()
    for row_no, row in enumerate(rows[1:], start=2):
        if _is_empty_row(row):
            continue
        code = _norm_code(_cell(row, index_map["stock_material"]))
        if code == "":
            errors.append("%s第%d行物料编码不能为空" % (label, row_no))
            continue
        if code in seen:
            errors.append(
                "%s第%d行物料编码「%s」重复，库存数据不得重复登记"
                % (label, row_no, code)
            )
            continue
        seen.add(code)
        qty = _parse_int(
            _cell(row, index_map["stock_qty"]),
            errors,
            "%s第%d行「%s」库存数量" % (label, row_no, code),
            positive=False,
        )
        if qty is not None:
            stock[code] = qty


def _parse_demand(rows, index_map, demand_rows, errors):
    label = "Sheet「订单需求」"
    seen = set()
    for row_no, row in enumerate(rows[1:], start=2):
        if _is_empty_row(row):
            continue
        order = ""
        order_idx = index_map.get("demand_order")
        if order_idx is not None and order_idx < len(row) \
                and not _is_blank(row[order_idx]):
            order = str(row[order_idx]).strip()
        code = _norm_code(_cell(row, index_map["demand_material"]))
        if code == "":
            errors.append("%s第%d行需求物料编码不能为空" % (label, row_no))
            continue
        qty = _parse_int(
            _cell(row, index_map["demand_qty"]),
            errors,
            "%s第%d行「%s」需求数量" % (label, row_no, code),
            positive=True,
        )
        if qty is None:
            continue
        fingerprint = (order, code, qty)
        if fingerprint in seen:
            errors.append(
                "%s第%d行与前面的订单需求完全重复(订单号=%s, 物料=%s, "
                "数量=%d)"
                % (label, row_no, order or "(空)", code, qty)
            )
            continue
        seen.add(fingerprint)
        demand_rows.append(
            {"row_no": row_no, "order": order, "code": code, "qty": qty}
        )


def _parse_bom(rows, index_map, bom_rows, errors):
    label = "Sheet「BOM」"
    seen = set()
    for row_no, row in enumerate(rows[1:], start=2):
        if _is_empty_row(row):
            continue
        parent = _norm_code(_cell(row, index_map["bom_parent"]))
        child = _norm_code(_cell(row, index_map["bom_child"]))
        if parent == "":
            errors.append("%s第%d行父件编码不能为空" % (label, row_no))
            continue
        if child == "":
            errors.append("%s第%d行子件编码不能为空" % (label, row_no))
            continue
        usage = _parse_int(
            _cell(row, index_map["bom_usage"]),
            errors,
            "%s第%d行「%s->%s」用量" % (label, row_no, parent, child),
            positive=True,
        )
        if usage is None:
            continue
        edge = (parent, child)
        if edge in seen:
            errors.append(
                "%s第%d行父子件组合「%s -> %s」重复"
                % (label, row_no, parent, child)
            )
            continue
        seen.add(edge)
        bom_rows.append(
            {
                "row_no": row_no,
                "parent": parent,
                "child": child,
                "usage": usage,
            }
        )


def _validate_references(
    materials, stock, demand_rows, bom_rows, sheet_keys, errors
):
    for code in stock:
        if code not in materials:
            errors.append(
                "Sheet「%s」引用了物料表中不存在的物料编码「%s」"
                % (sheet_keys["stock"], code)
            )
    for row in demand_rows:
        if row["code"] not in materials:
            errors.append(
                "Sheet「%s」第%d行引用了物料表中不存在的物料编码「%s」"
                % (
                    sheet_keys["demand"],
                    row["row_no"],
                    row["code"],
                )
            )
    for row in bom_rows:
        if row["parent"] not in materials:
            errors.append(
                "Sheet「%s」第%d行父件编码「%s」在物料表中不存在"
                % (sheet_keys["bom"], row["row_no"], row["parent"])
            )
        if row["child"] not in materials:
            errors.append(
                "Sheet「%s」第%d行子件编码「%s」在物料表中不存在"
                % (sheet_keys["bom"], row["row_no"], row["child"])
            )


def _detect_cycle(materials, bom_rows):
    adjacency = defaultdict(list)
    for row in bom_rows:
        adjacency[row["parent"]].append(row["child"])
    color = {}  # 0 = white, 1 = gray (on stack), 2 = black (done)
    for start in sorted(materials.keys()):
        if color.get(start, 0) != 0:
            continue
        color[start] = 1
        stack = [start]
        iterators = [iter(sorted(adjacency.get(start, ())))]
        while iterators:
            advanced = False
            for nxt in iterators[-1]:
                state = color.get(nxt, 0)
                if state == 1:
                    idx = stack.index(nxt)
                    return stack[idx:] + [nxt]
                if state == 0:
                    color[nxt] = 1
                    stack.append(nxt)
                    iterators.append(
                        iter(sorted(adjacency.get(nxt, ())))
                    )
                    advanced = True
                    break
            if not advanced:
                color[stack.pop()] = 2
                iterators.pop()
    return None


# ---------------------------------------------------------------------------
# Calculation
# ---------------------------------------------------------------------------

def _calculate(
    materials, stock, demand_rows, bom_rows,
    plan_sheet_name, detail_sheet_name,
):
    children = defaultdict(dict)   # parent -> {child: usage}
    parents = defaultdict(list)    # child -> [parent]
    for row in bom_rows:
        children[row["parent"]][row["child"]] = row["usage"]
        parents[row["child"]].append(row["parent"])

    topo_order, levels = _topological_order(materials, children)

    gross = defaultdict(int)
    detail_rows = []
    for row in demand_rows:
        gross[row["code"]] += row["qty"]
        detail_rows.append(
            [
                "独立需求",
                row["order"],
                "",
                row["code"],
                "",
                "",
                row["qty"],
            ]
        )

    planned = {}
    balance = {}
    for code in topo_order:
        gross_qty = gross[code]
        on_hand = stock.get(code, 0)
        net_qty = gross_qty - on_hand
        if net_qty < 0:
            net_qty = 0
        lot = materials[code]["lot"]
        if net_qty > 0:
            planned_qty = ((net_qty + lot - 1) // lot) * lot
        else:
            planned_qty = 0
        planned[code] = planned_qty
        balance[code] = on_hand + planned_qty - gross_qty
        for child in sorted(children.get(code, {}).keys()):
            usage = children[code][child]
            required = planned_qty * usage
            if required > 0:
                gross[child] += required
                detail_rows.append(
                    [
                        "BOM展开",
                        "",
                        code,
                        child,
                        usage,
                        planned_qty,
                        required,
                    ]
                )

    plan_rows = []
    for code in sorted(materials.keys(), key=lambda c: (levels[c], c)):
        gross_qty = gross.get(code, 0)
        on_hand = stock.get(code, 0)
        net_qty = max(0, gross_qty - on_hand)
        plan_rows.append(
            [
                code,
                materials[code]["name"],
                levels[code],
                gross_qty,
                on_hand,
                net_qty,
                materials[code]["lot"],
                planned[code],
                balance[code],
            ]
        )

    output = Book(
        OrderedDict(
            [
                (plan_sheet_name, [PLAN_HEADER] + plan_rows),
                (detail_sheet_name, [DETAIL_HEADER] + detail_rows),
            ]
        )
    )
    return output


def _topological_order(materials, children):
    indegree = dict((code, 0) for code in materials)
    parent_set = defaultdict(set)
    for parent, child_map in children.items():
        for child in child_map:
            indegree[child] += 1
            parent_set[child].add(parent)

    ready = sorted(code for code in materials if indegree[code] == 0)
    order = []
    levels = {}
    while ready:
        code = ready.pop(0)
        order.append(code)
        parent_levels = [levels[p] for p in parent_set.get(code, ())]
        levels[code] = max(parent_levels) + 1 if parent_levels else 0
        for child in sorted(children.get(code, {}).keys()):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    return order, levels


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_empty_row(row):
    return all(_is_blank(cell) for cell in row)


def _cell(row, index):
    if index is None or index >= len(row):
        return None
    return row[index]


def _norm_code(value):
    if _is_blank(value):
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value).strip()
    return str(value).strip()


def _parse_int(value, errors, where, positive):
    """Return an exact non-negative (or positive) integer.

    Booleans, fractional numbers and unparsable strings are rejected.
    Integers and integral floats/strings are accepted.
    """
    if _is_blank(value):
        errors.append("%s不能为空" % where)
        return None
    if isinstance(value, bool):
        errors.append("%s必须是整数，收到布尔值 %s" % (where, value))
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            errors.append("%s必须是整数，收到小数 %s" % (where, value))
            return None
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = int(text)
        except ValueError:
            errors.append("%s必须是整数，收到 %r" % (where, value))
            return None
    else:
        errors.append("%s必须是整数，收到 %r" % (where, value))
        return None
    if positive:
        if number <= 0:
            errors.append("%s必须为正整数，收到 %d" % (where, number))
            return None
    elif number < 0:
        errors.append("%s不能为负数，收到 %d" % (where, number))
        return None
    return number

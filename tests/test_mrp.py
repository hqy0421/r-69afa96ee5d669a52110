"""
Tests for multi-level material requirements planning (pyexcel.mrp)
"""

import unittest

import pyexcel as pe
from pyexcel.book import Book
from pyexcel.mrp import (
    DEFAULT_SHEET_NAMES,
    DETAIL_SHEET,
    MRPError,
    PLAN_SHEET,
    build_material_plan,
)

from .base import clean_up_files


MATERIAL_HEADER = ["物料编码", "物料名称", "批量"]
BOM_HEADER = ["父件编码", "子件编码", "用量"]
DEMAND_HEADER = ["订单号", "需求物料编码", "需求数量"]
STOCK_HEADER = ["物料编码", "库存数量"]


def make_book(materials, bom=None, demand=None, stock=None):
    return pe.get_book(
        bookdict={
            DEFAULT_SHEET_NAMES["material"]: [MATERIAL_HEADER] + materials,
            DEFAULT_SHEET_NAMES["bom"]: [BOM_HEADER] + (bom or []),
            DEFAULT_SHEET_NAMES["demand"]: [DEMAND_HEADER]
            + (demand or []),
            DEFAULT_SHEET_NAMES["stock"]: [STOCK_HEADER] + (stock or []),
        }
    )


def plan_map(book):
    rows = book[PLAN_SHEET].to_array()
    header, body = rows[0], rows[1:]
    index = header.index("物料编码")
    return dict((row[index], row) for row in body)


class TestBuildMaterialPlan(unittest.TestCase):
    def setUp(self):
        # A (lot 1) -> B (lot 2), 1 each; B -> C (lot 100), 2 each
        self.book = make_book(
            materials=[
                ["A", "成品", 1],
                ["B", "组件", 2],
                ["C", "螺丝", 100],
            ],
            bom=[["A", "B", 1], ["B", "C", 2]],
            demand=[["SO1", "A", 5]],
            stock=[["C", 50]],
        )

    def test_basic_multi_level_explosion(self):
        plan = build_material_plan(self.book)
        self.assertIsInstance(plan, Book)
        self.assertEqual(plan.sheet_names(), [PLAN_SHEET, DETAIL_SHEET])
        rows = plan_map(plan)
        # header
        self.assertEqual(
            plan[PLAN_SHEET].to_array()[0],
            [
                "物料编码", "物料名称", "层级", "毛需求", "库存", "净需求",
                "批量", "生产或采购量", "预计结存",
            ],
        )
        # A: gross 5, no stock, build 5
        self.assertEqual(rows["A"][3:], [5, 0, 5, 1, 5, 0])
        # B: gross 5 follows A's planned 5, lot 2 -> 6, balance 1
        self.assertEqual(rows["B"][3:], [5, 0, 5, 2, 6, 1])
        # C: gross = 6 * 2 = 12 (planned output of B, not net 5)
        #    stock 50 covers it, no build, balance 38
        self.assertEqual(rows["C"][3:], [12, 50, 0, 100, 0, 38])
        # levels
        self.assertEqual((rows["A"][2], rows["B"][2], rows["C"][2]),
                         (0, 1, 2))

    def test_lower_level_follows_planned_quantity(self):
        # 3 units of A; B lot 2 -> build 4; C usage 2 -> gross 8
        book = make_book(
            materials=[["A", "", 1], ["B", "", 2], ["C", "", 1]],
            bom=[["A", "B", 1], ["B", "C", 2]],
            demand=[["SO", "A", 3]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["B"][7], 4)
        self.assertEqual(rows["C"][3], 8)
        self.assertEqual(rows["C"][8], 0)

    def test_shared_component_stock_deducted_once(self):
        # Both A1 and A2 consume B; B stock 3 must be deducted once
        # against the merged gross requirement, not twice.
        book = make_book(
            materials=[
                ["A1", "", 1], ["A2", "", 1], ["B", "", 10],
            ],
            bom=[["A1", "B", 1], ["A2", "B", 1]],
            demand=[["SO1", "A1", 5], ["SO2", "A2", 4]],
            stock=[["B", 3]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["B"][3], 9)       # merged gross 5 + 4
        self.assertEqual(rows["B"][4], 3)       # stock shown once
        self.assertEqual(rows["B"][5], 6)       # net = 9 - 3
        self.assertEqual(rows["B"][7], 10)      # rounded to lot 10
        self.assertEqual(rows["B"][8], 4)       # balance 3 + 10 - 9

    def test_detail_sheet_contents_and_reconciliation(self):
        plan = build_material_plan(self.book)
        detail = plan[DETAIL_SHEET].to_array()
        self.assertEqual(
            detail[0],
            [
                "需求类型", "订单号/来源", "父件编码",
                "子件/需求物料编码", "单位用量", "父件投产量",
                "毛需求数量",
            ],
        )
        self.assertEqual(
            detail[1:],
            [
                ["独立需求", "SO1", "", "A", "", "", 5],
                ["BOM展开", "", "A", "B", 1, 5, 5],
                ["BOM展开", "", "B", "C", 2, 6, 12],
            ],
        )
        # every detail quantity reconciles to the plan gross requirement
        rows = plan_map(plan)
        gross_from_detail = {}
        for record in detail[1:]:
            code = record[3]
            gross_from_detail[code] = (
                gross_from_detail.get(code, 0) + record[6]
            )
        for code, row in rows.items():
            self.assertEqual(gross_from_detail.get(code, 0), row[3])

    def test_multiple_demand_orders_are_merged(self):
        book = make_book(
            materials=[["A", "", 1]],
            demand=[["SO1", "A", 5], ["SO2", "A", 7]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["A"][3], 12)
        plan = build_material_plan(book)
        detail = plan[DETAIL_SHEET].to_array()[1:]
        self.assertEqual(len(detail), 2)
        self.assertEqual(
            sorted(record[1] for record in detail), ["SO1", "SO2"]
        )

    def test_stock_covers_all_demand(self):
        book = make_book(
            materials=[["A", "", 10]],
            demand=[["SO", "A", 4]],
            stock=[["A", 4]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["A"][5], 0)      # net
        self.assertEqual(rows["A"][7], 0)      # nothing to build
        self.assertEqual(rows["A"][8], 0)      # balance

    def test_excess_stock_becomes_balance(self):
        book = make_book(
            materials=[["A", "", 1]],
            demand=[["SO", "A", 3]],
            stock=[["A", 10]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["A"][5], 0)
        self.assertEqual(rows["A"][7], 0)
        self.assertEqual(rows["A"][8], 7)

    def test_lot_rounding_ceiling(self):
        book = make_book(
            materials=[["A", "", 25]],
            demand=[["SO", "A", 26]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["A"][7], 50)
        self.assertEqual(rows["A"][8], 24)

    def test_default_lot_is_one_when_column_missing(self):
        book = pe.get_book(
            bookdict={
                "物料": [["物料编码"], ["A"]],
                "BOM": [["父件编码", "子件编码", "用量"]],
                "订单需求": [
                    ["订单号", "需求物料编码", "需求数量"],
                    ["SO", "A", 3],
                ],
                "库存": [["物料编码", "库存数量"]],
            }
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["A"][6], 1)
        self.assertEqual(rows["A"][7], 3)

    def test_material_without_demand_still_listed_with_zeros(self):
        rows = plan_map(build_material_plan(self.book))
        self.assertIn("C", rows)
        # add an unused material to the master
        book = make_book(
            materials=[["A", "", 1], ["Z", "闲置件", 5]],
            demand=[["SO", "A", 1]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["Z"][3:], [0, 0, 0, 5, 0, 0])

    def test_integral_floats_accepted_decimals_rejected(self):
        book = make_book(
            materials=[["A", "", 1.0]],
            demand=[["SO", "A", 2.0]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["A"][7], 2)
        # fractional quantities must fail
        bad = make_book(
            materials=[["A", "", 1]],
            demand=[["SO", "A", 2.5]],
        )
        with self.assertRaises(MRPError) as ctx:
            build_material_plan(bad)
        self.assertIn("整数", str(ctx.exception))

    def test_integer_strings_are_accepted(self):
        book = make_book(
            materials=[["A", "", "3"]],
            demand=[["SO", "A", "4"]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["A"][6], 3)
        self.assertEqual(rows["A"][7], 6)

    def test_blank_rows_are_skipped(self):
        book = make_book(
            materials=[["A", "", 1], ["", "", ""]],
            demand=[["SO", "A", 2], ["", "", ""]],
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(set(rows.keys()), {"A"})

    def test_all_quantities_are_python_ints(self):
        plan = build_material_plan(self.book)
        for row in plan[PLAN_SHEET].to_array()[1:]:
            for cell in row[2:]:
                self.assertIsInstance(cell, int)
        for row in plan[DETAIL_SHEET].to_array()[1:]:
            for cell in row[4:]:
                if cell != "":
                    self.assertIsInstance(cell, int)


class TestMRPErrors(unittest.TestCase):
    def _assert_error(self, book, *fragments):
        with self.assertRaises(MRPError) as ctx:
            build_material_plan(book)
        message = str(ctx.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)

    def test_missing_sheet(self):
        book = pe.get_book(
            bookdict={
                "物料": [MATERIAL_HEADER, ["A", "", 1]],
                "BOM": [BOM_HEADER],
                "订单需求": [DEMAND_HEADER],
            }
        )
        self._assert_error(book, "缺少 Sheet", "库存")

    def test_empty_sheet(self):
        book = pe.get_book(
            bookdict={
                "物料": [],
                "BOM": [BOM_HEADER],
                "订单需求": [DEMAND_HEADER],
                "库存": [STOCK_HEADER],
            }
        )
        self._assert_error(book, "为空", "物料")

    def test_missing_required_column(self):
        book = pe.get_book(
            bookdict={
                "物料": [["物料编码"], ["A"]],
                "BOM": [BOM_HEADER],
                "订单需求": [["订单号", "需求物料编码", "需求数量"]],
                "库存": [["库存数量"]],  # missing 物料编码
            }
        )
        self._assert_error(book, "缺少必需列", "物料编码")

    def test_duplicate_header_column(self):
        book = pe.get_book(
            bookdict={
                "物料": [["物料编码", "物料编码"], ["A", "B"]],
                "BOM": [BOM_HEADER],
                "订单需求": [DEMAND_HEADER],
                "库存": [STOCK_HEADER],
            }
        )
        self._assert_error(book, "表头列名重复")

    def test_duplicate_material(self):
        book = make_book(
            materials=[["A", "", 1], ["A", "", 2]],
        )
        self._assert_error(book, "物料编码「A」重复")

    def test_duplicate_stock_row(self):
        book = make_book(
            materials=[["A", "", 1]],
            stock=[["A", 1], ["A", 2]],
        )
        self._assert_error(book, "库存数据不得重复登记")

    def test_duplicate_bom_edge(self):
        book = make_book(
            materials=[["A", "", 1], ["B", "", 1]],
            bom=[["A", "B", 1], ["A", "B", 2]],
        )
        self._assert_error(book, "父子件组合「A -> B」重复")

    def test_duplicate_demand_row(self):
        book = make_book(
            materials=[["A", "", 1]],
            demand=[["SO1", "A", 3], ["SO1", "A", 3]],
        )
        self._assert_error(book, "完全重复")
        # same material on different orders is NOT a duplicate
        ok = make_book(
            materials=[["A", "", 1]],
            demand=[["SO1", "A", 3], ["SO2", "A", 3]],
        )
        self.assertEqual(
            plan_map(build_material_plan(ok))["A"][3], 6
        )

    def test_invalid_reference_in_stock(self):
        book = make_book(
            materials=[["A", "", 1]],
            stock=[["GHOST", 5]],
        )
        self._assert_error(book, "不存在的物料编码「GHOST」")

    def test_invalid_reference_in_demand(self):
        book = make_book(
            materials=[["A", "", 1]],
            demand=[["SO", "GHOST", 1]],
        )
        self._assert_error(book, "不存在的物料编码「GHOST」")

    def test_invalid_reference_in_bom(self):
        book = make_book(
            materials=[["A", "", 1]],
            bom=[["A", "GHOST", 1]],
        )
        self._assert_error(book, "子件编码「GHOST」在物料表中不存在")
        book2 = make_book(
            materials=[["A", "", 1]],
            bom=[["GHOST", "A", 1]],
        )
        with self.assertRaises(MRPError) as ctx:
            build_material_plan(book2)
        self.assertIn("父件编码「GHOST」", str(ctx.exception))

    def test_circular_bom_self_loop(self):
        book = make_book(
            materials=[["A", "", 1]],
            bom=[["A", "A", 1]],
            demand=[["SO", "A", 1]],
        )
        self._assert_error(book, "BOM 存在循环引用", "A -> A")

    def test_circular_bom_three_nodes(self):
        book = make_book(
            materials=[["A", "", 1], ["B", "", 1], ["C", "", 1]],
            bom=[["A", "B", 1], ["B", "C", 1], ["C", "A", 1]],
        )
        self._assert_error(book, "BOM 存在循环引用", "->")

    def test_multiple_errors_are_reported_together(self):
        # duplicate material + bad reference + non-integer usage,
        # everything must surface in one batch error
        book = make_book(
            materials=[["A", "", 1], ["A", "", 1], ["B", "", 1]],
            bom=[["A", "GHOST", 1], ["A", "B", 1.5]],
        )
        with self.assertRaises(MRPError) as ctx:
            build_material_plan(book)
        message = str(ctx.exception)
        self.assertIn("3 个数据问题", message)
        self.assertIn("重复", message)
        self.assertIn("不存在", message)
        self.assertIn("整数", message)

    def test_negative_and_zero_quantities(self):
        book = make_book(
            materials=[["A", "", 0]],
        )
        self._assert_error(book, "正整数")
        book2 = make_book(
            materials=[["A", "", 1]],
            stock=[["A", -1]],
        )
        self._assert_error(book2, "不能为负数")

    def test_boolean_quantity_rejected(self):
        book = make_book(
            materials=[["A", "", True]],
        )
        self._assert_error(book, "布尔值")

    def test_empty_required_code(self):
        book = make_book(
            materials=[["", "", 1]],
        )
        self._assert_error(book, "物料编码不能为空")

    def test_no_partial_plan_on_error(self):
        bad = make_book(
            materials=[["A", "", 1], ["B", "", 1]],
            bom=[["A", "B", 1], ["B", "A", 1]],  # cycle
        )
        before = bad.to_dict()
        with self.assertRaises(MRPError):
            build_material_plan(bad)
        # source book untouched
        self.assertEqual(bad.to_dict(), before)

    def test_non_book_input(self):
        with self.assertRaises(MRPError):
            build_material_plan({"物料": []})


class TestCustomization(unittest.TestCase):
    def test_custom_sheet_and_column_names(self):
        book = pe.get_book(
            bookdict={
                "item": [
                    ["item_no", "desc", "moq"],
                    ["P", "part", 4],
                ],
                "structure": [["parent", "child", "per"]],
                "orders": [["order_no", "item", "qty"], ["SO9", "P", 5]],
                "onhand": [["item_no", "qty"], ["P", 1]],
            }
        )
        plan = build_material_plan(
            book,
            sheet_names={
                "material": "item",
                "bom": "structure",
                "demand": "orders",
                "stock": "onhand",
            },
            columns={
                "material_code": "item_no",
                "lot_size": "moq",
                "bom_parent": "parent",
                "bom_child": "child",
                "bom_usage": "per",
                "demand_order": "order_no",
                "demand_material": "item",
                "demand_qty": "qty",
                "stock_material": "item_no",
                "stock_qty": "qty",
            },
        )
        rows = plan_map(plan)
        # gross 5 - stock 1 = net 4, lot 4 -> build 4, balance 0
        self.assertEqual(rows["P"][3:], [5, 1, 4, 4, 4, 0])

    def test_alternative_builtin_column_aliases(self):
        # 物料编号 / 料号 / 库存量 are built-in aliases
        book = pe.get_book(
            bookdict={
                "物料": [["物料编号", "批量"], ["A", 1]],
                "BOM": [["父件", "子件", "单位用量"]],
                "订单需求": [
                    ["订单号", "料号", "毛需求数量"], ["SO", "A", 2],
                ],
                "库存": [["物料编号", "库存量"]],
            }
        )
        rows = plan_map(build_material_plan(book))
        self.assertEqual(rows["A"][3], 2)

    def test_whitespace_in_headers_and_codes(self):
        book = pe.get_book(
            bookdict={
                "物料": [[" 物料编码 ", "批量"], [" A ", 1]],
                "BOM": [BOM_HEADER],
                "订单需求": [DEMAND_HEADER, ["SO", "A", 2]],
                "库存": [STOCK_HEADER],
            }
        )
        rows = plan_map(build_material_plan(book))
        self.assertIn("A", rows)


class TestSourceImmutabilityAndExport(unittest.TestCase):
    def setUp(self):
        self.book = make_book(
            materials=[["A", "成品", 1], ["B", "组件", 2]],
            bom=[["A", "B", 1]],
            demand=[["SO1", "A", 5]],
            stock=[["B", 1]],
        )
        self.original = self.book.to_dict()

    def test_source_book_is_not_modified(self):
        plan = build_material_plan(self.book)
        self.assertEqual(self.book.to_dict(), self.original)
        self.assertEqual(
            set(self.book.sheet_names()),
            {"物料", "BOM", "订单需求", "库存"},
        )
        # plan is a separate object
        self.assertIsNot(plan, self.book)

    def test_repeated_calls_return_independent_books(self):
        plan1 = build_material_plan(self.book)
        plan2 = build_material_plan(self.book)
        self.assertEqual(
            plan1[PLAN_SHEET].to_array(), plan2[PLAN_SHEET].to_array()
        )
        self.assertIsNot(plan1, plan2)
        plan1[PLAN_SHEET].extend_rows([["X", "", 0, 0, 0, 0, 1, 0, 0]])
        self.assertNotEqual(
            plan1[PLAN_SHEET].to_array(),
            plan2[PLAN_SHEET].to_array(),
        )

    def test_round_trip_through_xlsx(self):
        filename = "mrp_plan_test.xlsx"
        try:
            plan = build_material_plan(self.book)
            plan.save_as(filename)
            reloaded = pe.get_book(file_name=filename)
            self.assertEqual(
                reloaded[PLAN_SHEET].to_array(),
                plan[PLAN_SHEET].to_array(),
            )
            self.assertEqual(
                reloaded[DETAIL_SHEET].to_array(),
                plan[DETAIL_SHEET].to_array(),
            )
        finally:
            clean_up_files([filename])

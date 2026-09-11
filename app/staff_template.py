"""Blank employee workbook matching the staff editor's seven data fields."""
import io

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

HEADERS = ("姓名", "部门", "工号", "入职日期", "生日（月日）", "在职状态", "用户 ID（open_id）")


def build():
    wb = Workbook()
    ws = wb.active
    ws.title = "员工资料"
    ws.append(HEADERS)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:G1"
    for cell, width in zip(ws[1], (20, 28, 20, 22, 22, 18, 44)):
        cell.font = Font(bold=True, color="44405E")
        cell.fill = PatternFill("solid", fgColor="F3F1FA")
        ws.column_dimensions[cell.column_letter].width = width
    ws.column_dimensions["C"].number_format = "@"
    ws.column_dimensions["D"].number_format = "yyyy-mm-dd"
    ws.column_dimensions["E"].number_format = "@"
    ws.column_dimensions["G"].number_format = "@"
    ws["D1"].comment = Comment("填写完整入职年月日，例如 2021-02-01；周年数按完整日期计算，满一年后触发。", "填写说明")
    ws["E1"].comment = Comment("只填写月日，例如 02-01 或 2月1日，不需要出生年份。兼容旧完整日期，推送只匹配月日；2月29日不在平年提前推送。", "填写说明")
    ws["C1"].comment = Comment("按文本填写以保留开头的零。更新现有员工时请保留原工号。", "填写说明")
    ws["F1"].comment = Comment("填写在职或离职。新增时留空默认在职；更新时留空保留原状态。", "填写说明")
    ws["G1"].comment = Comment("填写该员工在当前飞书应用中的 open_id（以 ou_ 开头），与姓名一一对应。不是工号、本地员工 ID 或飞书 user_id。留空保留原值；未绑定时连接飞书后按唯一姓名自动对应。重复 ID 或姓名不匹配时不会推送。", "填写说明")
    status = DataValidation(type="list", formula1='"在职,离职"', allow_blank=True)
    status.errorTitle, status.error = "在职状态无效", "请选择在职或离职。"
    status.showErrorMessage = True
    ws.add_data_validation(status)
    status.add("F2:F10001")
    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()

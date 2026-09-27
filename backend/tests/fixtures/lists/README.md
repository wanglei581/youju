# 真实政府名单的脱敏裁剪样本

取得日期：2026-09-27。使用项目 `PoliteFetcher` 抓取，遵守 robots 和单站限速。原件只在内存中处理；仓库内仅保存约 30 条企业字段，不保存联系人、电话等个人信息列。

| 文件 | 官方原文 | 处理方式 |
|---|---|---|
| `panjin_table.html` | [盘锦市科技局公示](https://kjj.panjin.gov.cn/2026_06/29_09/content-566832.html) | 取网页企业表格前 30 条，仅保留序号和企业名称。此公告是奖励公示，只用于验证 HTML 表格解析，标题规则应排除。 |
| `heilongjiang_30.xlsx` | [黑龙江省科技厅公示](https://kjt.hlj.gov.cn/kjt/c113908/202608/c00_31965158.shtml) | 取 XLSX 附件前 30 条，保留序号、企业名称、注册地。 |
| `heilongjiang_30.xls` | [黑龙江省科技厅公示](https://kjt.hlj.gov.cn/kjt/c113908/202608/c00_31965158.shtml) | 将同一份裁剪表重编码成旧版 XLS，验证旧格式读取。 |
| `shanghai_page1.pdf` | [上海市科委公告](https://stcsm.sh.gov.cn/zwgk/tzgs/gsgg/zhgsgg/20260710/ce6b24b93a774c1bafdfc347003e39e3.html) | 从 PDF 附件第 1 页取前 27 条企业名称，重新排版成小型表格 PDF；原 PDF 未入库、未提交。 |

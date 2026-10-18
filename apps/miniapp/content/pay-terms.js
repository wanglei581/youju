// 付费服务协议（批量工具 30 天、90 天，D-070）。定稿后更新正文、版本，并把 final 改为 true；
// final 为 false 时付费按钮不可用——协议没定稿就不能收费。草稿见 docs/合规/付费服务协议与退款规则_v2.md。
// 能开哪种发票（D-070：付款前要看得到）不写在这里，以服务端 YOUJU_PAY_INVOICE_TYPE 为准（购买页照它显示，
// 现网没填就不卖）；定稿时协议正文第四节写同一句。
module.exports = {
  version: 'pay-2026-10-draft',
  final: false,
  title: '付费服务协议',
  text: '【付费服务协议：待律师定稿】',
};

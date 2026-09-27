// 演示数据：来自 2026-09-26 的真实抓取和原文核对（与原型 v2 相同）。接口（#27）就绪后只在开发时使用。
// 截止日期写成 ISO 日期，剩余天数按当天实时计算；verified 表示已人工核对原文（未核对的不显示截止日期）。
module.exports = {
  "coverage": {
    "sources": 72,
    "provinces": 28,
    "regions": [
      {
        "name": "国家部委",
        "sources": 8,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "中国政府网 · 政策首页",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "国家发展和改革委员会 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "国家知识产权局 · 工作通知",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "国家统计局 · 最新发布（统计数据）",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "国家统计局 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "生态环境部 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "科学技术部 · 通知通告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "财政部 · 政策发布",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "北京",
        "sources": 4,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "北京市科学技术委员会、中关村科技园区管理委员会 · 政策文件",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "北京市科学技术委员会、中关村科技园区管理委员会 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "北京市经济和信息化局 · 政策文件",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "北京市经济和信息化局 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "天津",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "天津市工业和信息化局 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "天津市科学技术局 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "河北",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "河北省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "河北省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "山西",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "山西省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "山西省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "内蒙古",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "内蒙古自治区工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "内蒙古自治区科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "辽宁",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "辽宁省工业和信息化厅 · 通知通告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "辽宁省科学技术厅 · 通知通告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "吉林",
        "sources": 1,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "吉林省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "黑龙江",
        "sources": 3,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "黑龙江省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "黑龙江省科学技术厅 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "黑龙江省科学技术厅 · 科技计划通知",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "上海",
        "sources": 3,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "上海市科学技术委员会 · 办事通知告示",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "上海市科学技术委员会 · 通知告示",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "上海市经济和信息化委员会 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "江苏",
        "sources": 3,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "江苏省工业和信息化厅 · 公告公示",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "江苏省工业和信息化厅 · 文件通知",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "江苏省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "安徽",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "安徽省工业和信息化厅 · 文件公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "安徽省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "福建",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "福建省工业和信息化厅 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "福建省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "江西",
        "sources": 1,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "江西省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "山东",
        "sources": 4,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "山东省工业和信息化厅 · 公示",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "山东省工业和信息化厅 · 通知",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "山东省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "山东省科学技术厅 · 通知公告（另一入口）",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "河南",
        "sources": 3,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "河南省工业和信息化厅 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "河南省工业和信息化厅 · 文件通知",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "河南省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "湖北",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "湖北省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "湖北省经济和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "湖南",
        "sources": 3,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "湖南省工业和信息化厅 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "湖南省工业和信息化厅 · 文件通知",
            "lastUpdatedAt": null
          },
          {
            "name": "湖南省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "广东",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "广东省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "广东省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "海南",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "海南省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "海南省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "重庆",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "重庆市科学技术局 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "重庆市经济和信息化委员会 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "四川",
        "sources": 3,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "四川省科学技术厅 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "四川省科学技术厅 · 通知",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "四川省经济和信息化厅 · 通知",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "贵州",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "贵州省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "贵州省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "云南",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "云南省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "云南省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "西藏",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "西藏自治区科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "西藏自治区经济和信息化厅 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "陕西",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "陕西省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "陕西省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "青海",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "青海省工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "青海省科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "宁夏",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "宁夏回族自治区工业和信息化厅 · 公示公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "宁夏回族自治区科学技术厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      },
      {
        "name": "新疆",
        "sources": 2,
        "lastUpdatedAt": "2026-09-28T09:30:00+08:00",
        "columns": [
          {
            "name": "新疆维吾尔自治区工业和信息化厅 · 通知公告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          },
          {
            "name": "新疆维吾尔自治区科学技术厅 · 通知通告",
            "lastUpdatedAt": "2026-09-28T09:30:00+08:00"
          }
        ]
      }
    ],
    "notCovered": [
      {
        "name": "浙江",
        "reason": "正在接入"
      },
      {
        "name": "广西",
        "reason": "网站不允许程序访问"
      },
      {
        "name": "甘肃",
        "reason": "网站不允许程序访问"
      }
    ],
    "updatedAt": "2026-09-28T09:30:00+08:00"
  },
  "stats": {
    "week_total": 158,
    "apply": 25,
    "lists": 70,
    "other": 63,
    "provinces": 28,
    "sources": 72,
    "followed_new": 10
  },
  "home": [
    {
      "id": "ln",
      "kind": "申报通知",
      "status": null,
      "region": "辽宁 · 省级",
      "title": "辽宁省工业和信息化厅关于组织开展2026年三季度辽宁省制造业固定资产贷款贴息及担保补贴项目申报工作的通知",
      "issuer": "辽宁省工业和信息化厅",
      "date": "2026-09-24",
      "url": "https://gxt.ln.gov.cn/gxt/tztg/2026092416504833875/index.shtml",
      "deadline": {
        "date": "2026-10-18",
        "time": "",
        "verified": true
      }
    },
    {
      "id": "sh",
      "kind": "名单",
      "status": "公示",
      "region": "上海 · 省级",
      "title": "关于上海市2026年第六批拟入库科技型中小企业名单的公示",
      "issuer": "上海市科学技术委员会",
      "date": "2026-09-24",
      "url": "https://stcsm.sh.gov.cn/zwgk/tzgs/gsgg/zhgsgg/20260924/76934379206c4e5dbfac5fff53d0b257.html"
    },
    {
      "id": "jx",
      "kind": "申报通知",
      "status": null,
      "region": "江西 · 省级",
      "title": "关于组织申报2026年度江西省技术创新中心的通知",
      "issuer": "江西省科学技术厅",
      "date": "2026-09-24",
      "url": "https://kjt.jiangxi.gov.cn/jxskxjst/col/col27029/content/content_2103050611282489344.html",
      "deadline": {
        "date": "2026-10-23",
        "time": "17:00",
        "verified": true
      }
    },
    {
      "id": "gw",
      "kind": "政策",
      "status": null,
      "region": "国家",
      "title": "国务院办公厅关于印发《“高效办成一件事”2026年度第二批重点事项清单》的通知",
      "issuer": "国务院办公厅",
      "date": "2026-09-23",
      "url": "https://www.gov.cn/zhengce/content/202609/content_7081880.htm"
    },
    {
      "id": "bj",
      "kind": "名单",
      "status": "公布",
      "region": "北京 · 省级",
      "title": "北京市经济和信息化局关于公布2026年新创建北京市企业技术中心名单的通知",
      "issuer": "北京市经济和信息化局",
      "date": "2026-09-24",
      "url": "https://jxj.beijing.gov.cn/jxdt/tzgg/202609/t20260924_4879912.html"
    },
    {
      "id": "hb",
      "kind": "申报通知",
      "status": null,
      "region": "湖北 · 省级",
      "title": "省科技厅关于组织申报2026年部分省级技术创新计划项目的通知",
      "issuer": "湖北省科学技术厅",
      "date": "2026-09-23",
      "url": "https://kjt.hubei.gov.cn/kjdt/tzgg/202609/t20260922_6020453.shtml",
      "deadline": {
        "date": "2026-10-26",
        "time": "17:00",
        "verified": true
      }
    },
    {
      "id": "gd",
      "kind": "政策",
      "status": null,
      "region": "广东 · 省级",
      "title": "广东省工业和信息化厅 中国建设银行广东省分行印发产业培育“善建智造”综合金融服务方案的通知",
      "issuer": "广东省工业和信息化厅",
      "date": "2026-09-22",
      "url": "https://gdii.gd.gov.cn/zwgk/tzgg1011/content/post_4959711.html"
    },
    {
      "id": "sx",
      "kind": "名单",
      "status": "公布",
      "region": "山西 · 省级",
      "title": "山西省工业和信息化厅关于公布2026年专精特新中小企业名单的通知",
      "issuer": "山西省工业和信息化厅",
      "date": "2026-09-22",
      "url": "https://gxt.shanxi.gov.cn/xwdt/tzgg/202609/t20260922_10225637.shtml"
    }
  ],
  "search": {
    "q": "科技型中小企业",
    "total_30d": 35,
    "total_all": 59,
    "items": [
      {
        "id": "s0",
        "kind": "名单",
        "status": null,
        "region": "新疆 · 第4批",
        "title": "关于2026年第4批入库科技型中小企业名单的公告",
        "issuer": "新疆维吾尔自治区科学技术厅",
        "date": "2026-09-24",
        "url": "https://kjt.xinjiang.gov.cn/kjt/c100264/202609/0f846fea177a4092bc752abde9676f1c.shtml"
      },
      {
        "id": "s1",
        "kind": "名单",
        "status": "公示",
        "region": "山东 · 第4批",
        "title": "山东省2026年第4批拟入库科技型中小企业名单公示",
        "issuer": "山东省科学技术厅",
        "date": "2026-09-24",
        "url": "http://kjt.shandong.gov.cn/art/2026/9/24/art_13360_10330190.html"
      },
      {
        "id": "s2",
        "kind": "名单",
        "status": "公示",
        "region": "上海 · 第六批",
        "title": "关于上海市2026年第六批拟入库科技型中小企业名单的公示",
        "issuer": "上海市科学技术委员会",
        "date": "2026-09-24",
        "url": "https://stcsm.sh.gov.cn/zwgk/tzgs/gsgg/zhgsgg/20260924/76934379206c4e5dbfac5fff53d0b257.html"
      },
      {
        "id": "s3",
        "kind": "名单",
        "status": null,
        "region": "贵州 · 第三批",
        "title": "贵州省2026年第三批入库科技型中小企业名单公告",
        "issuer": "贵州省科学技术厅",
        "date": "2026-09-23",
        "url": "http://kjt.guizhou.gov.cn/xwzx/tzgg_73876/202609/t20260923_90935041.html"
      },
      {
        "id": "s4",
        "kind": "名单",
        "status": "公示",
        "region": "安徽 · 第3批",
        "title": "关于安徽省2026年第3批拟入库科技型中小企业名单的公示",
        "issuer": "安徽省科学技术厅",
        "date": "2026-09-23",
        "url": "https://kjt.ah.gov.cn/kjzx/tzgg/123510901.html"
      },
      {
        "id": "s5",
        "kind": "名单",
        "status": "公示",
        "region": "内蒙古 · 第七批",
        "title": "关于公示内蒙古自治区2026年第七批拟入库科技型中小企业名单的通知",
        "issuer": "内蒙古自治区科学技术厅",
        "date": "2026-09-23",
        "url": "https://kjt.nmg.gov.cn/kjdt/tzgg/202609/t20260923_2958019.html"
      },
      {
        "id": "s6",
        "kind": "名单",
        "status": "公示",
        "region": "天津 · 第五批",
        "title": "天津市2026年第五批拟入库科技型中小企业名单公示",
        "issuer": "天津市科学技术局",
        "date": "2026-09-20",
        "url": "https://kxjs.tj.gov.cn/ZWGK4143/TZGG2079/202609/t20260920_7378027.html"
      }
    ]
  },
  "lists": {
    "week_total": 70,
    "items": [
      {
        "id": "l0",
        "kind": "名单",
        "status": "公布",
        "region": "北京 · 省级",
        "title": "北京市经济和信息化局关于公布2026年新创建北京市企业技术中心名单的通知",
        "issuer": "北京市经济和信息化局",
        "date": "2026-09-24",
        "url": "https://jxj.beijing.gov.cn/jxdt/tzgg/202609/t20260924_4879912.html"
      },
      {
        "id": "l1",
        "kind": "名单",
        "status": "公示",
        "region": "上海 · 省级",
        "title": "关于上海市2026年第六批拟入库科技型中小企业名单的公示",
        "issuer": "上海市科学技术委员会",
        "date": "2026-09-24",
        "url": "https://stcsm.sh.gov.cn/zwgk/tzgs/gsgg/zhgsgg/20260924/76934379206c4e5dbfac5fff53d0b257.html"
      },
      {
        "id": "l2",
        "kind": "名单",
        "status": "公布",
        "region": "西藏 · 省级",
        "title": "关于公布西藏自治区2026年度绿色制造名单的通知",
        "issuer": "西藏自治区经济和信息化厅",
        "date": "2026-09-24",
        "url": "http://jxt.xizang.gov.cn/xzweb/detail?id=3808&channelId=9&categoryId=69"
      },
      {
        "id": "l3",
        "kind": "名单",
        "status": "公示",
        "region": "新疆 · 省级",
        "title": "2026年度自治区级绿色工厂拟定名单公示",
        "issuer": "新疆维吾尔自治区工业和信息化厅",
        "date": "2026-09-23",
        "url": "https://gxt.xinjiang.gov.cn/gxt/tzgg/202609/fc11014f78974fa7a37ee1677fcdc2e0.shtml"
      },
      {
        "id": "l4",
        "kind": "名单",
        "status": "公布",
        "region": "山西 · 省级",
        "title": "山西省工业和信息化厅关于公布2026年专精特新中小企业名单的通知",
        "issuer": "山西省工业和信息化厅",
        "date": "2026-09-22",
        "url": "https://gxt.shanxi.gov.cn/xwdt/tzgg/202609/t20260922_10225637.shtml"
      },
      {
        "id": "l5",
        "kind": "名单",
        "status": "公示",
        "region": "河南 · 省级",
        "title": "关于河南省2026年度第三批拟更名和重大变化高新技术企业的公示",
        "issuer": "河南省科学技术厅",
        "date": "2026-09-22",
        "url": "http://kjt.henan.gov.cn/2026/09-22/3431626.html"
      }
    ]
  },
  "calendar": [
    {
      "id": "nx",
      "due": "2026-10-16",
      "time": "",
      "region": "宁夏 · 科技厅",
      "kind": "申报通知",
      "title": "自治区科技厅关于组织申报2027年度自治区科技金融补助的通知",
      "what": "第一批推荐截止",
      "hasDetail": false,
      "verified": true
    },
    {
      "id": "ln",
      "due": "2026-10-18",
      "time": "",
      "region": "辽宁 · 工信厅",
      "kind": "申报通知",
      "title": "辽宁省工业和信息化厅关于组织开展2026年三季度辽宁省制造业固定资产贷款贴息及担保补贴项目申报工作的通知",
      "what": "企业申报截止（原文：逾期申报平台自动关闭）· 市级推荐 10月20日",
      "hasDetail": true,
      "verified": true
    },
    {
      "id": "jx",
      "due": "2026-10-23",
      "time": "17:00",
      "region": "江西 · 科技厅",
      "kind": "申报通知",
      "title": "关于组织申报2026年度江西省技术创新中心的通知",
      "what": "依托单位网上提交截止 · 推荐部门审核 10月29日 17:00",
      "hasDetail": false,
      "verified": true
    },
    {
      "id": "hb",
      "due": "2026-10-26",
      "time": "17:00",
      "region": "湖北 · 科技厅",
      "kind": "申报通知",
      "title": "省科技厅关于组织申报2026年部分省级技术创新计划项目的通知",
      "what": "申报单位网上申报截止 · 推荐单位审核 10月28日 17:00",
      "hasDetail": false,
      "verified": true
    },
    {
      "id": "gz",
      "due": "2026-10-29",
      "time": "18:00",
      "region": "贵州 · 科技厅",
      "kind": "申报通知",
      "title": "省科技厅关于发布2026年决策科学项目申报指南的通知",
      "what": "主管部门系统网上填报截止 · 9月29日 9:00 起开放",
      "hasDetail": false,
      "verified": true
    }
  ],
  "follow": {
    "week_start": "9月21日",
    "new_total": 10,
    "subs": [
      {
        "name": "专精特新",
        "desc": "全国 · 申报通知、名单与公示",
        "n": 6,
        "icon": "bulletpoint",
        "tone": "orange"
      },
      {
        "name": "高新技术企业",
        "desc": "全国 · 申报通知、名单与公示",
        "n": 2,
        "icon": "bulletpoint",
        "tone": "orange"
      },
      {
        "name": "辽宁 · 工信部门",
        "desc": "申报通知",
        "n": 1,
        "icon": "task-checked",
        "tone": "blue"
      },
      {
        "name": "山东 · 科技型中小企业",
        "desc": "名单与公示",
        "n": 1,
        "icon": "bulletpoint",
        "tone": "orange"
      }
    ],
    "changes": [
      {
        "id": "ln",
        "kind": "申报通知",
        "status": null,
        "region": "辽宁 · 省级",
        "title": "辽宁省工业和信息化厅关于组织开展2026年三季度辽宁省制造业固定资产贷款贴息及担保补贴项目申报工作的通知",
        "issuer": "辽宁省工业和信息化厅",
        "date": "2026-09-24",
        "url": "https://gxt.ln.gov.cn/gxt/tztg/2026092416504833875/index.shtml",
        "deadline": {
          "date": "2026-10-18",
          "time": "",
          "verified": true
        },
        "label": "辽宁 · 工信部门"
      },
      {
        "id": "sx",
        "kind": "名单",
        "status": "公布",
        "region": "山西 · 省级",
        "title": "山西省工业和信息化厅关于公布2026年专精特新中小企业名单的通知",
        "issuer": "山西省工业和信息化厅",
        "date": "2026-09-22",
        "url": "https://gxt.shanxi.gov.cn/xwdt/tzgg/202609/t20260922_10225637.shtml",
        "label": "专精特新"
      },
      {
        "id": "bjhi",
        "kind": "名单",
        "status": null,
        "region": "",
        "title": "关于北京市2026年第四批高新技术企业更名的通知",
        "issuer": "北京市科学技术委员会、中关村科技园区管理委员会",
        "date": "2026-09-22",
        "url": "https://kw.beijing.gov.cn/zwgk/tzgg/202609/t20260922_4876631.html",
        "label": "高新技术企业"
      },
      {
        "id": "hn",
        "kind": "申报通知",
        "status": null,
        "region": "",
        "title": "关于组织开展2026年第二批专精特新中小企业申报和复核工作的通知",
        "issuer": "海南省工业和信息化厅",
        "date": "2026-09-21",
        "url": "https://iitb.hainan.gov.cn/iitb/tzgg/202609/5e951788f1024a86a684fb8aa528ab77.shtml",
        "label": "专精特新"
      }
    ]
  },
  "files": [
    {
      "badge": "XLSX",
      "name": "科技型中小企业 · 近 30 天",
      "meta": "35 份 · Excel · 21 KB · 今天 10:42",
      "expired": false
    },
    {
      "badge": "XLSX",
      "name": "本周新增 · 全部类型",
      "meta": "158 份 · Excel · 64 KB · 今天 08:30",
      "expired": false
    },
    {
      "badge": "DOCX",
      "name": "辽宁贷款贴息及担保补贴申报要点",
      "meta": "1 份 · Word · 18 KB · 9月19日",
      "expired": true
    }
  ],
  "detail": {
    "ln": {
      "id": "ln",
      "kind": "申报通知",
      "tags": [
        "申报通知",
        "资金支持"
      ],
      "region": "辽宁 · 省级",
      "title": "辽宁省工业和信息化厅关于组织开展2026年三季度辽宁省制造业固定资产贷款贴息及担保补贴项目申报工作的通知",
      "issuer": "辽宁省工业和信息化厅",
      "date": "2026-09-24",
      "url": "https://gxt.ln.gov.cn/gxt/tztg/2026092416504833875/index.shtml",
      "site": "辽宁省工业和信息化厅网站",
      "host": "gxt.ln.gov.cn",
      "deadline": {
        "label": "企业申报截止",
        "date": "2026-10-18",
        "time": "",
        "notes": [
          "市级推荐截止 10月20日 周二",
          "原文：逾期申报平台自动关闭"
        ],
        "verified": true
      },
      "points": [
        [
          "发文机关",
          "辽宁省工业和信息化厅"
        ],
        [
          "文号",
          "辽工信投资〔2026〕130号"
        ],
        [
          "申报方向",
          "产业结构优化升级项目；智改数转和绿色低碳发展项目等"
        ],
        [
          "申报对象",
          "制造业企业（以 GB/T 4754-2017 为准，含软件和信息技术服务业）"
        ],
        [
          "适用地区",
          "辽宁省"
        ]
      ],
      "excerpt": {
        "section": "二、申报条件",
        "text": "（一）申报单位原则上应为制造业企业（以GB/T 4754-2017《国民经济行业分类》为准，含软件和信息技术服务业）。（二）财务管理机构和制度健全，纳税记录良好。（三）诚信守法，近三年未发生重大（含）以上安全、环保、质量事故（事件），未被纳入严重失信主体名单。"
      },
      "attachments": [
        {
          "ext": "PDF",
          "name": "附件1 辽宁省制造业固定资产贷款贴息及担保补贴实施细则（2026年）"
        },
        {
          "ext": "XLSX",
          "name": "附件2 银行贷款贴息项目审核情况汇总表"
        }
      ],
      "checked": "2026-09-26"
    }
  }
};

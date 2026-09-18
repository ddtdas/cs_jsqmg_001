---
name: supply-chain
description: 供应链反查（OSINT 顺藤摸瓜）。输入骗子/目标给出的网址或概念，多维度反向侦察，摸出完整关联供应链（域名→IP→WHOIS→证书→关联域名→子域名→社交账号→邮箱→资金链路→团伙图谱）。用于金丝雀蜜罐 SupplyChain 页面与 agent 反诈侦查。
---

# 供应链反查（Supply Chain OSINT）skill

> 用途：根据骗子或目标**给出的一个网址/概念**（如诈骗链接 `http://xxx.com/...`、微信号、QQ、手机号、平台昵称、投资概念），沿 OSINT 链路**顺藤摸瓜**，把整条"供应链"（域名、服务器、账号、收款渠道、关联团伙）全部摸出来，形成可展示的关联图谱。
> 参考方法论：netmcp（网络分析/威胁情报）、mcp_dfir（取证链）、redteam OSINT 逆查实践。
> 合规红线（D7）：只读公开/自有信息；不发起攻击性扫描（不做主动漏洞探测、不做未授权爆破）；结果仅用于个人反诈侦查取证。

## 1. 输入形态识别（先分类再反查）

| 输入 | 示例 | 首步动作 |
|---|---|---|
| URL/域名 | `http://abc-trade.com/invest` | 提取根域名 → DNS 解析 |
| IP | `103.xx.xx.xx` | 直接 IP 情报 |
| 微信号/QQ | `vx: wxid_xxx` | 关联搜索 |
| 手机号 | `138xxxx` | 号码归属/注册平台 |
| 平台昵称 | 知乎/微博昵称 | 平台内搜索 + 账号画像 |
| 概念词 | "稳赚不赔量化交易" | 全网检索相关诈骗站点/话术模板 |

## 2. 反查链路（顺藤摸瓜：逐层扩展，每层产出证据项）

```
输入（网址/概念）
  └─ L1 域名解析：DNS A/AAAA/CNAME/NS/MX/TXT → 解析 IP、CDN 标识、邮件服务器、SPF/DMARC
  ├─ L2 IP 情报：IP 归属(ASN/ISP)、地理位置、开放端口(被动)、历史解析、反查同 IP 其他域名(Reverse DNS / IP→domain)
  ├─ L3 WHOIS：注册人/注册邮箱/注册商/创建时间/过期时间 → 邮箱反查关联域名（同注册人/同邮箱）
  ├─ L4 证书透明度 (CT)：crt.sh 查询 → 同一证书关联的其他域名、历史证书
  ├─ L5 子域名枚举（被动）：crt.sh + DNS 字典 → 关联子域名（后台/API/邮件等）
  ├─ L6 关联域名扩展：相似域名（typosquatting）、同 ICP 备案主体、同模板站点指纹
  ├─ L7 账号与社交：域名/邮箱/手机号在搜索引擎/社交平台的关联账号；平台昵称画像
  ├─ L8 资金链路：支付/收款账户线索（USDT 地址、银行卡、支付宝/微信收款名）
  └─ L9 团伙聚合：上述证据按"注册邮箱/注册人/IP/收款账户"聚类 → 团伙图谱
```

## 3. 每层的输出格式（统一证据项）

```json
{
  "layer": "L3_WHOIS",
  "query": "abc-trade.com",
  "findings": [
    {"type": "registrant_email", "value": "admin@whois-proxy.com", "note": "WHOIS 注册邮箱"},
    {"type": "creation_date", "value": "2025-01-01", "note": "新注册域名（诈骗典型特征）"}
  ],
  "next_queries": ["admin@whois-proxy.com", "registrant_name: John"]
}
```

- 每层必须输出 `next_queries`（下一层可反查的线索），实现"顺藤摸瓜"
- `findings` 全部来自公开信息，标注来源（dns/dnsleak/whois/crt.sh/搜索引擎/平台）

## 4. 顺藤摸瓜循环（核心）

1. 从输入提取**种子线索**（域名/邮箱/IP/手机号/账号）
2. 对每条线索执行 L1→L9 各层反查（可用可用工具/API/搜索引擎）
3. 新线索入队列（去重，最多 N 轮 = 3 层深度）
4. 每次命中更新图谱：节点（域名/IP/邮箱/账号/收款）+ 边（解析/注册/共现/资金）
5. 输出：`供应链图谱`（ECharts graph 数据：nodes + edges）+ `证据链` + `疑点摘要`（新域名/代理邮箱/海外 IP/高相似模板 = 高度可疑信号）

## 5. 疑点信号（诈骗供应链特征）

- 域名注册时间 < 6 个月
- WHOIS 使用隐私代理 / 一次性邮箱
- 解析 IP 为海外高防/频繁更换
- 证书自签名或 Let's Encrypt 短期
- 相似域名批量注册（同注册邮箱）
- 收款账户（USDT/银行卡）与域名主体不一致

## 6. 输出交付（SupplyChain 页面/agent 使用）

1. `summary`：输入 → 摸出的供应链摘要（几层、多少节点/证据）
2. `graph`：`{nodes: [{id, name, type, layer, risk}], edges: [{source, target, rel}]}`
3. `evidence`：全部证据项（带来源与时间戳）
4. `next_steps`：建议的下一步反查（未深挖的线索）
5. `compliance_note`：合规声明（只读公开信息，处置权归官方）

## 7. 工具建议（无 GPU/无外网特殊工具时）

- DNS：`nslookup` / `Resolve-DnsName`（Windows 自带）/ 公共 DNS-over-HTTPS
- WHOIS：`whois` 命令（若装）/ 公共 WHOIS API
- CT 证书：`https://crt.sh/?q=%25.<domain>`（公开，可 curl）
- 反查/搜索引擎：web_search / 搜索引擎 site: 语法
- IP 情报：公共 IP API（如 ip-api.com json，可 curl）
- 被动子域名：crt.sh + 字典（本地简单词表）
- 所有请求保持低频、只读，遵守合规红线

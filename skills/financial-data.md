# 财务数据获取与交叉验证规范

本规范适用于所有涉及企业财务数据的研究。**每个关键数据必须来自两个独立来源，误差>1%须标记。**

---

## 数据源优先级

### 美股（PDD、腾讯ADR、网易ADR等）

| 优先级 | 来源 | URL | 获取方式 |
|--------|------|-----|---------|
| 1（主） | **macrotrends** | macrotrends.net/stocks/charts/{ticker} | 直接访问，无需注册 |
| 2（副） | **stockanalysis** | stockanalysis.com/stocks/{ticker}/financials | 直接访问，无需注册 |
| 原始一手 | SEC EDGAR | sec.gov/cgi-bin/browse-edgar | 10-K / 10-Q 原文 |

### 港股（腾讯0700、网易9999、美团3690等）

| 优先级 | 来源 | URL | 获取方式 |
|--------|------|-----|---------|
| 1（主） | **aastocks** | aastocks.com/tc/stocks/analysis/company-fundamental | 直接访问 |
| 2（副） | **macrotrends**（ADR代码） | 腾讯用TCEHY，网易用NTES | 直接访问 |
| 原始一手 | HKEX披露易 | hkexnews.hk | 年报PDF |

### A股（三七互娱、吉比特等）

| 优先级 | 来源 | URL | 获取方式 |
|--------|------|-----|---------|
| 1（主） | **东方财富** | eastmoney.com → 搜股票代码 → 财务报表 | 直接访问，或用下方接口 |
| 2（副） | **巨潮资讯** | cninfo.com.cn | 原始年报/季报PDF |
| 行情副源 | **腾讯行情** | qt.gtimg.cn | `tools/ashare_data.py`（零依赖） |

#### 东方财富接口速查（2026-08 实测可用）

批量筛选与逐日估值序列都能拿到，全部零依赖 curl 即可。**主机优先 `push2delay`——`push2` 对连续请求限流严，实测整轮批量拉取会超时。**

| 用途 | 接口 | 备注 |
|---|---|---|
| 全市场批量行情+财务 | `push2delay.eastmoney.com/api/qt/clist/get` | `fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23` 覆盖沪深京 5,548 只；`pz=200` 分页 |
| 单只快照 | `push2delay.eastmoney.com/api/qt/stock/get` | `secid=0.000538`（0=深 1=沪） |
| **逐日 PE/PB/PS 历史** | `datacenter-web.eastmoney.com/api/data/v1/get` `reportName=RPT_VALUEANALYSIS_DET` | 约 2,088 个交易日（8.6 年），算估值分位用 |
| 年报核心财务 | `datacenter.eastmoney.com/securities/api/data/get` `type=RPT_F10_FINANCE_MAINFINADATA&sty=ALL` | 含扣非、经营现金流、ROIC、总股本 |
| **法定公告** | `np-anotice-stock.eastmoney.com/api/security/ann` | `columns[].column_name` 是交易所官方分类 |
| 资讯检索 | `search-api-web.eastmoney.com/search/jsonp` | ⚠️ 大盘股信噪比极差，见下 |

#### ⚠️ 陷阱一：同名字段在不同接口含义不同（必读）

**这是本规范里最容易出错的一点。** 同一只伊利股份：

| 字段 | `clist`（批量） | `stock/get`（单只） |
|---|---|---|
| `f173` | 涨跌相关（1.19） | **ROE = 9.44** |
| `f184` | 涨跌相关（-5.94） | **营收同比 = 5.47%** |
| `f186` / `f188` | **96% 为空** | 毛利率 / 资产负债率 |

**批量接口的正确字段**（已用长江电力 f46=+30.50%、格力 f46=+3.01% 对报告原文逐项验证）：

```
f12=代码 f14=名称 f2=现价 f20=总市值 f23=PB f25=年初至今%
f37=ROE  f40=营收 f41=营收同比% f45=净利 f46=净利同比%
f49=毛利率 f57=资产负债率 f100=行业 f115=PE(TTM) f129=净利率 f130=PS
f133=股息率 f221=报告期(如20260331)
```

**单只接口**：`f173=ROE f184=营收同比 f186=毛利率 f188=资产负债率`。

→ **换接口就必须重新验证字段**。方法：取一只已有报告的公司，把接口返回值与报告原文逐项比对，全中才可信。

#### ⚠️ 陷阱二：报告期混用会毁掉横向比较

`f221` 是报告期。2026-08 实测全市场：**4,952 只为 20260331（一季报）、261 只已出中报（20260630）、334 只缺失**。中报口径的 ROE 约为一季报的两倍，混在一起排序会系统性偏袒早披露的公司。**筛选必须先按 `f221` 统一口径。**

#### ⚠️ 陷阱三：分页静默截断会系统性高估估值分位

`RPT_VALUEANALYSIS_DET` 要翻 5 页（`pageSize=500`）。实测中途一页取数失败、脚本 `break` 退出，只拿到 1,000 天就算分位——**云南白药 PE 分位由正确的 2.1% 变成 4.4%**。残缺的历史序列缺的是早年的高估值段，**必然把当前分位算高**。

→ **取数函数必须"失败即抛错"，绝不返回残缺序列**；并校验实际行数 ≈ `pages × pageSize`。

#### 估值分位工具的验证方法

写完分位工具后，用**已有报告的已知结论**做反向验证，全中才可用：

| 验证项 | 报告原值 | 实测 |
|---|---|---|
| 伊利 PE / PB / PS 分位（8.6y） | 5.4% / 3.6% / 14.4% | 5.4% / 3.5% / 14.3% ✅ |
| 格力 PB 分位（8.6y） | 5.9% | 5.8% ✅ |
| 格力 PE 分位（**近5年**） | 51.2% | 50.9% ✅ |

⚠️ **窗口口径会实质改变结论**：格力 PE 在 8.6 年窗口是 30.2%，近 5 年窗口是 50.9%。**引用分位必须注明窗口**，本 repo 的既有报告混用过两种窗口。

#### 新闻 vs 公告：大盘股必须用公告

实测直近 7 日，`search-api-web` 关键词检索对云南白药/长江电力返回 30 篇，**社固有新闻 0 篇**——全是 ETF/指数成分股定型文、机构调研名单、两融数据、他公司报道中的顺带提及。

硬事件（离任/并购/立案/分红变更）**法定必须在公告披露**，故应以 `np-anotice-stock` 为主。其 `columns[].column_name` 是官方分类，可直接分级：
- 高信号：`高管人员任职变动` `重大资产重组` `关联交易` `立案调查` `监管措施` `分配预案` `权益变动` `月度经营情况`
- 纯噪音：`调研活动`（直近 50 件中占 28 件，最大噪音源）、`独立董事*声明` `法律意见书` `内部控制报告` `ESG公告`

### 台股（台积电2330、联发科2454、大立光3008等）

| 优先级 | 来源 | URL | 获取方式 |
|--------|------|-----|---------|
| 1（主） | **FinMind API** | api.finmindtrade.com | `tools/twstock_data.py`（零依赖脚本，见下） |
| 2（副） | **Goodinfo台湾股市资讯网** | goodinfo.tw/tw/StockDetail.asp?STOCK_ID={代码} | 直接访问 |
| 原始一手 | 公开资讯观测站（MOPS） | mops.twse.com.tw | 财报原文/月营收公告 |

**FinMind 取数工具**（分析台股时优先调用，输出自带市值验算）：

```bash
python3 tools/twstock_data.py quote 2330        # 最新行情 + PER/PBR/殖利率 + 市值验算
python3 tools/twstock_data.py valuation 2330    # 估值指标 + PER一年区间 + 52周高低
python3 tools/twstock_data.py financials 2330   # 近5年年度核心财务（营收/毛利率/归母净利/EPS/ROE）
python3 tools/twstock_data.py revenue 2330      # 近13个月月营收及同比
python3 tools/twstock_data.py dividend 2330     # 近年股利政策（现金/股票股利、除息日）
python3 tools/twstock_data.py search 台積        # 搜索股票代码（注意台股名称为繁体）
```

台股特别注意：

1. **货币单位是新台币（TWD）**，与港币/人民币/美元混排时必须显式标注，跨市场对比先统一换算
2. **月营收是台股独有优势**：上市柜公司每月10日前强制披露上月营收，是跟踪基本面拐点最快的公开信号，earnings-review/thesis-tracker 类分析应优先利用（`revenue` 子命令）
3. FinMind 损益表为**单季值**，工具已自动加总为年度值；不足4季的年份会标注"仅前N季累计"
4. FinMind 未注册可直接用（有小时级限额）。注册后的 API token **只存本机、严禁提交到 git**，工具按优先级自动读取：①环境变量 `FINMIND_TOKEN`；②本地文件 `local/finmind_token.txt`（`local/` 已被 `.gitignore` 永久排除，把 token 单独一行写入该文件即可）。token 不得出现在报告、skill、commit 中
5. 交叉验证：FinMind 数值与 Goodinfo（或 macrotrends 上的 ADR，如 TSM）对照，误差规则同下；台积电等有 ADR 的公司注意 ADR 与台股原股的汇率/存托比率差异（1 TSM ADR = 5 股 2330）

---

## 执行规范

### 第一步：获取数据

对每个财务指标（收入、净利润、毛利率、经营现金流、资产负债率等），分别从**来源1**和**来源2**取数。

### 第二步：误差计算与标记

```
误差率 = |来源1数值 - 来源2数值| / 来源1数值 × 100%
```

| 误差 | 处理方式 |
|------|---------|
| ≤ 1% | ✅ 一致，取来源1数值，标注两个来源 |
| 1% ~ 5% | ⚠️ 标记"数据存在差异"，注明两个数值，说明可能原因（汇率/会计口径） |
| > 5% | ❌ 标记"数据存在重大差异"，必须查原始财报核实，不得直接使用 |

### 第三步：数据呈现格式

每个关键数据必须按以下格式标注：

```
收入：1,239亿元 ✅
  - macrotrends: 1,241亿元
  - stockanalysis: 1,237亿元
  - 误差: 0.3%
```

差异示例：
```
净利润：245亿元 ⚠️ 数据存在差异
  - macrotrends: 245亿元（GAAP）
  - stockanalysis: 278亿元（Non-GAAP）
  - 误差: 13.5% — 原因：会计口径不同（GAAP vs Non-GAAP）
```

---

## 常见差异原因（不一定是数据错误）

| 原因 | 说明 |
|------|------|
| GAAP vs Non-GAAP | 最常见，尤其是利润类数据 |
| 汇率换算 | 港币/人民币/美元换算时间点不同 |
| 财年定义 | 自然年 vs 财年（如苹果财年10月结束） |
| 合并口径 | 是否含少数股东权益 |
| 数据更新滞后 | 某平台尚未更新最新一期财报 |

---

## 特别规则

1. **未上市公司**（米哈游、莉莉丝等）：只有一手数据来源时，数据前标记 `[估计]`，不执行交叉验证
2. **季度数据 vs 年度数据**：优先使用年度数据做交叉验证，季度数据部分来源可能有滞后
3. **原始财报优先**：若两个来源均与原始财报（10-K/年报PDF）不符，以原始财报为准，标记来源错误

---

## 股价与复权（历史序列必读）

价格有三种口径，混用会让历史股价位置、长期涨幅、历史估值分位全部失真：

| 口径 | 含义 | 用途 |
|------|------|------|
| 不复权 | 实际成交价，除权除息日跳空 | 仅用于"当前时点"快照 |
| 前复权 | 以最新价为基准回调历史价 | 历史股价对比、N年涨幅、历史PE band 一律用它 |
| 后复权 | 以上市首日为基准前推 | 计算历史总回报/年化收益 |

规则：

1. 涉及历史价格的分析统一用**前复权**，且同一分析内**不得混用**复权与不复权来源。
2. 当前市值/当前PE 用**当前实际股价 × 当前总股本**即可，与复权无关——复权只影响历史序列。
3. 跨越拆股/大比例送转的每股指标（历史EPS、历史股价），必须复权还原后再同比。
4. 总回报/年化收益需计入分红（后复权已含），只看价格涨幅会低估。
5. 增发/回购后市值验算以最新总股本为准（`financial_rigor.py verify-market-cap` 偏差>5% 会提示核对）。

---

## 快速索引

| 场景 | 主要来源 | 备用来源 |
|------|---------|---------|
| PDD / 拼多多 | macrotrends.net/stocks/charts/PDD | stockanalysis.com/stocks/pdd |
| 腾讯 | macrotrends.net/stocks/charts/TCEHY | aastocks（0700.HK） |
| 网易 | macrotrends.net/stocks/charts/NTES | aastocks（9999.HK） |
| 三七互娱 | eastmoney.com（002555） | cninfo.com.cn |
| 吉比特 | eastmoney.com（603444） | cninfo.com.cn |
| Nintendo | macrotrends.net/stocks/charts/NTDOY | stockanalysis.com/stocks/ntdoy |
| Capcom | macrotrends（CCOEY） | stockanalysis（CCOEY） |
| 台积电 | tools/twstock_data.py（2330） | goodinfo.tw / macrotrends（TSM，注意1 ADR=5股） |
| 联发科 | tools/twstock_data.py（2454） | goodinfo.tw |

---

## 本 repo 的取数工具

| 工具 | 用途 | 备注 |
|---|---|---|
| `tools/ashare_data.py` | A股行情/年报/估值（腾讯行情+东财） | `quote` / `financials` / `valuation` / `search` |
| `tools/twstock_data.py` | 台股（FinMind） | 输出自带市值验算 |
| `tools/financial_rigor.py` | **市值验算/估值验算/三情景/精确计算** | 报告中的计算一律走它，禁止心算 |
| `tools/report_audit.py` | 报告数字抽检（15% 随机） | 准出流程，`extract` → 人工取数 → `verdict` |
| `tools/watch_alert.py` | 价格线监控 → Slack | 见 skill `ai-berkshire-ops` |
| `tools/news_watch.py` | 公告+新闻监控 → Slack | 同上 |

> `financial_rigor.py three-scenario` 的 `--growth` 收的是**小数**（0.105 = 10.5%），
> 传 10.5 会被当成 1050%，得出荒谬的目标价。实测踩过。

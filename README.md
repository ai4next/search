# search

联网深度研究技能。**零第三方依赖**（只用 Python 标准库），无需 API Key。

```bash
python3 scripts/search.py "固态电池产业化进展"
```

产出的不是链接列表，而是**每条要点都带 `[n]` 出处的证据报告**。

## 它做什么

```
问题
 ├─ 规划：拆成主题面（定义/原理/现状/对比/应用/数据/风险/方法/观点/反例）
 ├─ 检索：每个子查询 × 多个引擎，并行
 ├─ 挑选：引擎权重 + 排名 + 多引擎印证 + 域名权威度 → 决定抓哪些页面
 ├─ 抓取：下载正文，抽正文（去导航/广告/页脚）
 ├─ 打分：BM25 相关性 + 权威度 + 时效性 + 正文厚度 + 印证数
 ├─ 缺口：哪些主题面还没证据？正文里冒出哪些新实体？
 ├─ 追问：针对缺口生成下一轮查询（可多轮）
 └─ 产出：抽取式摘要（每句带出处）+ 分歧 + 缺口 + 来源清单
```

## 快速开始

```bash
# 默认：2 轮、抓正文、Markdown 报告（5~30 秒）
python3 scripts/search.py "固态电池产业化进展"

# 最快：只检索不抓正文（2~5 秒）
python3 scripts/search.py "向量数据库" -r 1 --no-fetch -f brief

# 更深
python3 scripts/search.py "Transformer 架构的局限" -r 3 -n 20 -b 6

# 落盘全部证据
python3 scripts/search.py "RAG 的局限" -o ./out/rag

# 用自己规划的子查询（复杂问题效果显著更好）
python3 scripts/search.py "如何选型向量数据库" \
  -q "向量数据库 对比 HNSW IVF PQ" "向量数据库 成本 运维 实践"

# 离线自检（92 项，不联网）
python3 scripts/search.py --test
```

## 成本旋钮

深度搜索会真的下载页面，**默认配置不便宜**。按需调：

| 场景 | 命令 | 耗时 |
|---|---|---|
| 只要链接和摘要 | `-r 1 --no-fetch -f brief` | 2~5 秒 |
| 要正文但不用追问 | `-r 1` | 5~15 秒 |
| **默认** | （无参数） | 5~30 秒 |
| 深挖 | `-r 3 -k 20 -n 20` | 30~90 秒 |

输出格式：`-f markdown`（默认，给人读）· `-f json`（agent 加工）·
`-f brief`（要点+来源，最省上下文）。

## 安装

这是一个 **DSH 技能**。推荐用 `npx skills` 安装——它会自动放到 DSH 扫描的技能根目录，
目录名也按 frontmatter 的 `name` 取好。没有构建步骤，没有第三方依赖（纯标准库，Python 3.8+）。

### npx 安装（推荐）

```bash
# 装到当前项目（<项目根>/.agents/skills/search/）
npx skills add ai4next/search

# 装到用户级，所有项目可用（~/.agents/skills/search/）
npx skills add ai4next/search -g
```

### 验证安装

```bash
# 1. 文件在位
ls ~/.agents/skills/search/SKILL.md

# 2. 能跑（92 项离线自检，不联网）
python3 ~/.agents/skills/search/scripts/search.py --test

# 3. 能搜
python3 ~/.agents/skills/search/scripts/search.py "向量数据库" -r 1 --no-fetch -f brief
```

装好后，技能会以 **`search`** 出现在会话的技能目录里，无需重启。

### 发现规则（手动安装前先看，避免白装）

DSH 只扫描技能根目录的**第一层**，两种形态：

```
<技能根>/<目录名>/SKILL.md      ← 目录包（本仓库用这种）
<技能根>/<名字>.md              ← 单文件
```

**嵌套的 `**/SKILL.md` 不会被发现。** 也就是说本仓库的目录必须**直接**位于技能根下，
不能再套一层。技能名取自 SKILL.md 的 frontmatter，与目录名无关。

被扫描的技能根（按优先级，数字小的优先）：

| 优先级 | 路径 | 作用范围 |
|---|---|---|
| 100 | `<项目根>/.dsh/skills` | 当前项目 |
| 200 | `<项目根>/.agents/skills` | 当前项目 |
| 300 | `customSkillDirs`（配置项） | 自定义 |
| 400 | `<DSH_HOME>/skills` | 当前用户 |
| 500 | `~/.agents/skills` | 当前用户 |

- `<项目根>` = 最近的含 `.git` 的祖先目录；没有则用当前工作目录
- `~/.agents` 可用环境变量 `DSH_AGENTS_HOME` 覆盖
- 技能根**被监听**，新增/改名/删除无需重启；**软链接会被跟随**

`npx skills add` 默认装的 `<项目根>/.agents/skills/`（rank 200），
加 `-g` 装的 `~/.agents/skills/`（rank 500），都在上表内。

### 两个坑

- **别把仓库嵌太深**：`~/.agents/skills/foo/search/` 不会被发现，
  必须是 `~/.agents/skills/search/`。
- **在本仓库里工作时**，项目根就是本仓库，此时 `.agents/skills` 指的是
  `<本仓库>/.agents/skills` —— 仓库根目录的 SKILL.md **不会**被自动发现。
  想在本仓库内可用，按方式一装到用户级，或在仓库内建
  `.agents/skills/search` 软链指向仓库自身。

## 引擎（16 个）

**可靠**（JSON API / 稳定解析）：arXiv、Crossref、OpenAlex、Wikipedia(中/英)、
GitHub、Stack Overflow、Hacker News、必应中文、必应国际、百度

**尽力而为**：360、搜狗、Brave、Mojeek、Ecosia

```bash
python3 scripts/search.py --list-engines
```

意图路由自动选引擎：`general / tech / academic / finance / news / social /
knowledge / privacy / advanced`。

## 几个不显然但重要的设计

- **检索请求不套 robots.txt，抓正文严格遵守。** 向搜索引擎发查询等价于
  用户在搜索框输入；而抓取第三方页面是爬取。混为一谈会让技能完全无法工作
  （Bing/Baidu 的 robots 都禁止 `/search`）。
- **先挑后抓。** 检索命中可能上百条，抓取是最慢最易被反爬的一步，
  所以先用多信号挑出最值得的 N 条。
- **中文术语用邻接熵发现，不用词频。** 靠词频会选出 "态电池产" 这种跨词碎片
  （它和 "固态电池" 词频一样高，因为总一起出现）；邻接熵能识别出它不是独立词。
- **英文库自动剥离中文。** `"python asyncio 超时处理"` → `"python asyncio"`，
  否则 GitHub/arXiv 的召回直接归零。
- **分歧检测只认特征词。** 共享话题词（"固态电池"）不算分歧，
  否则任意两句都会被判成分歧——那比没有这一节更糟。
- **摘要硬去重。** 网页常把同一句放在摘要块和正文各一次，只靠 MMR 惩罚
  不够，会重复输出。

## 配置

`config.yaml` 的 `deep_search:` 段可设默认值，命令行参数优先。

```bash
python3 scripts/search.py "查询" --show-config   # 查看生效配置
```

## 项目结构

```
SKILL.md              技能定义（agent 读这个）
config.yaml           配置（YAML，PyYAML 缺失时用内置解析器）
scripts/
  search.py           唯一入口
  ds/                 内核（全部零依赖）
    net.py            HTTP：编码嗅探、gzip、限速、缓存、robots
    dom.py            迷你 DOM + CSS 子集选择器
    extract.py        正文抽取（readability 精简版）
    engines.py        引擎注册表 + 意图路由
    planner.py        查询分解 + 缺口追问
    rank.py           去重 / BM25 排序 / 抽取式摘要 / 分歧检测
    report.py         证据包与 Markdown 渲染
    pipeline.py       多轮编排
    text.py           分词 / SimHash / URL 规范化 / 术语发现
    config.py         配置读取
test/
  test_deep_search.py 离线自检（92 项）
```

## 已知限制

- 知乎、百度百科、百家号的 `robots.txt` 禁止抓取 → 会被礼貌跳过（计入 warnings），
  这类页面只能拿到搜索摘要，拿不到正文
- DuckDuckGo、Yahoo 在部分网络下不可达 → 已从注册表移除
- 依赖第三方站点可用性；某引擎失败不影响整体，报告会如实标注
- 报告要点是**抽取式**（原句摘录），不是结论；综合判断由使用者完成

## License

MIT

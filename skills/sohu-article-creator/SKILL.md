---
name: sohu-article-creator
description: 网易号作者文章选题、抓取、AI 重写并导出搜狐号 docx 的项目内工作流。当用户说「抓一下」「抓取网易号」「抓最新文章」「看下作者更新」「选稿」「采用这几篇」「重写后导出」「导出 docx/word」，或提到 authors.txt、网易号作者主页、已抓取/已采用状态时使用。只适用于本项目 authors.txt + workspaces/ 缓存 + 搜狐号的流水线，不用于通用网页抓取、普通写作润色或其他平台文档导出。
---

# 搜狐文章创作

这个技能把「网易号选稿 → 抓正文 → AI 独立重写 → 保留原图编排 → 每篇导出 docx」串成一条固定流水线。用户说「抓一下」时只抓作者的最新文章列表，等用户选号后才继续抓正文和成稿。

## 路径约定

- **项目根目录**：包含 `.claude/skills/sohu-article-creator/SKILL.md` 的目录（通常也是 git 仓库根）。开始前先确认当前目录；如果不是，先 `cd` 到项目根目录。所有命令都在项目根目录执行。
- 技能真实目录是 `.claude/skills/sohu-article-creator`；为兼容已有配置，命令里统一写 `skills/sohu-article-creator/...`（它是指向真实目录的软链）。如果该软链不存在，把命令中的这个前缀替换成 `.claude/skills/sohu-article-creator`。
- 作者列表：项目根目录的 `authors.txt`，兼容用户常见的笔误文件名 `authros.txt`。两个文件都存在时以 `authors.txt` 为准，脚本会给出提示。
- 数据根目录：`./workspaces/`（脚本默认 `当前目录/workspaces`；在项目根目录执行时 `--root workspaces` 可省略，但关键命令建议显式写）；最终成稿：`workspaces/output/<导出当天 yyyyMMdd>/`。
- 不要 `cd` 到技能目录；不要修改 `content.txt`、`images/` 或 `references/writing_rules.md`。

## 硬性规则

1. **「抓一下」只抓列表，不抓正文。** 整个流程只有两处需要等用户输入：首次缺作者列表时提供 URL，以及选稿。除此之外不要停下来问「是否继续」。
2. **批量抓正文必须传 `-n 0`。** `crawl_article.py` 默认 `-n 5`，跨作者选稿时会把后面的文章截断。
3. **已抓取的正文直接复用，不重复请求。** 通过 `--skip-existing` 实现；不要为了“新鲜”重复下载。
4. **已采用的文章默认跳过。** 只有用户明确说「重做 / 重新导出」时，才同时给 `select_articles.py` 和 `export_article.py` 加 `--force`。
5. **列表阶段不写 articles 表。** 不要手工往状态库插记录，也不要把列表条目当成已抓取。
6. **标题只来自状态库里的原文标题。** docx 文件名用原标题（清理非法字符、超长截断）；docx 正文不包含标题。重写文件不写标题，不生成新标题，不改标题。
7. **`rewrite.txt` 只写正文。** 一行一个自然段；不写标题、小标题、分析、改写说明、来源链接或图片 Markdown。
8. **每篇独立 docx。** 单篇失败继续处理其余文章，最后统一汇报；不要因为一篇失败中断整批。
9. **用户采用后直接完成。** 不要逐篇问「是否继续」；只有选号有歧义或全部已采用时才追问一次。

## 状态标记

| 标记 | 含义 |
|---|---|
| `[新]` | 没有可用抓取记录，需要抓正文 |
| `[已抓取]` | 状态库有成功记录且 `content.txt` 存在；可复用缓存直接重写 |
| `[已采用][已抓取]` | 已成功导出且正文不含标题、`state=1`；默认不再处理 |
| `[抓取失败]` | 上次抓取/解析失败；重新选择会重试 |

编号由 `list_latest.py` 当次生成的快照决定。不要自己重排、重编号或重新计算。

## 第 0 步：确认作者列表

```bash
python3 skills/sohu-article-creator/scripts/author_store.py check --json
```

根据退出码处理：

- **退出码 0**：有有效作者。如果 `warnings` 非空，简短说明使用的是哪个文件，然后进入第 1 步。
- **退出码 3**：作者列表不存在或为空。向用户要网易号作者主页 URL，给一个格式示例：

  > 请提供一个网易号作者主页 URL，形如 `https://www.163.com/dy/media/T1668942583212.html`（不是文章页）。可以一次给多个，每行一个。

  收到后执行：

  ```bash
  python3 skills/sohu-article-creator/scripts/author_store.py add "<URL>" --json
  ```

  成功（退出码 0）后立即继续第 1 步，不要再等额外确认。若退出码 2，把 `errors` 里的原因告诉用户并重新索取；不要静默跳过。
- **退出码 2**：文件里有非法行且没有有效作者。展示 `invalid` 中的行号和原因，请用户修正或重新提供 URL。

如果用户给的是文章页（URL 含 `/dy/article/`），说明需要作者主页（`/dy/media/T....html`），不要把文章页写进作者列表。

## 第 1 步：抓一下

```bash
python3 skills/sohu-article-creator/scripts/list_latest.py -n 5 --root workspaces
```

- 把脚本 **stdout** 的编号列表和标记说明展示给用户：保留作者分组、全局编号、标题、时间和所有状态标记；不要重排、不要删减、不要重新编号。作者文件路径和快照路径可以放在列表末尾作为附注。请求日志在 stderr，不需要展示，除非有作者抓取失败。
- 列表脚本会把快照写到 `workspaces/.state/latest_list.json`。选稿只认这份快照。
- 展示完后只问一次：

  > 请回复要采用的编号（如 `1,3,5` / `1-3` / `全部`）。已采用的文章默认跳过；要重做请说明。

- **此时不要抓正文。** 如果用户只是回复编号，直接进入第 2 步；如果用户又说了一次「抓一下」，重新生成列表和快照，以最新一次为准。

退出码处理：

- `0`：正常列出，继续。
- `1`：所有作者都抓取失败或都没有文章。报告具体错误，不要继续抓正文。
- `2`：作者文件/参数有问题。展示 stderr 的错误。
- `3`：没有作者配置。回到第 0 步。

## 第 2 步：解析选稿

支持这些说法：

- `1,3,5` / `1 3 5` / `1、3、5` / `第1、3篇`
- `1-3` / `1到3`
- `全部` / `all`
- `荔子言 1,3`：某个作者的局部编号，此时用 `--author "荔子言"`。局部编号是该作者在本次列表中的第 1、2、3 篇，不是前面的全局编号。

如果用户用标题指代，先在 `latest_list.json` 里唯一定位到编号；标题重复或找不到时，展示当前列表请用户重新选。

```bash
python3 skills/sohu-article-creator/scripts/select_articles.py "1,3,5" --json
# 作者局部编号：
python3 skills/sohu-article-creator/scripts/select_articles.py "1,3" --author "荔子言" --json
# 用户明确说“重做/重新导出”：
python3 skills/sohu-article-creator/scripts/select_articles.py "1,3" --force --json
```

- 脚本会把选稿写到 `workspaces/.state/selection.json`。
- 默认跳过已采用文章，`skipped_adopted` 里会列出。若 `count=0` 且都是已采用，告诉用户「这些已采用，默认跳过；要重做请说重做」，不要自行加 `--force`。
- 如果选号越界或语法不对，脚本退出码 2；展示错误并请用户重新选，不要猜。

## 第 3 步：抓正文

```bash
python3 skills/sohu-article-creator/scripts/crawl_article.py \
  -i workspaces/.state/selection.json \
  --skip-existing -n 0 --root workspaces
```

- `-n 0` 必须保留。
- 输出「没有需要抓取的新文章」是正常情况，说明全部命中缓存，继续。
- 抓取结束后，从 `selection.json` 的 `articles` 里取出文章 ID，查询状态和目录：

  ```bash
  python3 skills/sohu-article-creator/scripts/state_store.py \
    --root workspaces show <ID1> <ID2> <ID3> --json
  ```

- 对每篇检查：
  - `missing` 里的 ID：抓取失败，记为失败，不要猜目录。
  - `cached=false` 或 `error` 非空：记为失败，不要重写或导出。
  - `cached=true`：可以继续重写。JSON 里的 `dir` 是文章目录的绝对路径，`dir_path` 是相对 `workspaces/` 的路径；读写文件用 `dir`。

## 第 4 步：逐篇重写

先读一次写作规则：

```text
skills/sohu-article-creator/references/writing_rules.md
```

然后对每一篇可用的文章：

1. 读取完整 `<dir>/content.txt`（`dir` 来自上一步 `state_store show --json` 的 JSON）。
2. 按写作规则在**内部**完成分析：文章类型、核心选题、目标读者、主要情绪、可发挥的角度、原文的薄弱处。不要把分析输出到对话或文件。
3. 用 Write 工具写 `<dir>/rewrite.txt`：
   - 只写完整正文，不写标题、不写 H1/H2、不写「分析」「改写说明」「创作思路」。
   - 一行一个自然段；不要把同一个自然段手动折成多行。
   - 不要写图片 Markdown、原文链接、来源、作者署名。
   - 不要照搬原句、不要逐段对应改写、不要机械同义词替换；要按写作规则重新构思，写得像独立创作。
   - 不要编造事实、数据、采访或亲身经历。
4. 标题不要写进 `rewrite.txt`。导出脚本从状态库取原标题用于 docx 文件名，转换 docx 时移除 md 开头的标题；最终正文不包含标题。

如果原文以图片为主、文字很少，按实际内容做力所能及的重写，不要硬编；必要时可以用 Read 工具查看 `images/` 里的关键图片帮助理解，但不要把图片路径写进正文（插图由导出脚本统一处理）。在最终报告里说明。

## 第 5 步：导出 docx

对每篇 `cached=true` 的文章执行：

```bash
python3 skills/sohu-article-creator/scripts/export_article.py <ARTICLE_ID> --root workspaces
```

- 不要直接调用 `img2md.py` 或 `make_docx.py`；`export_article.py` 负责插图、docx、移除正文标题和标记已采用。
- 脚本会把正文误加的 H1、与标题重复的首行、图片 Markdown 剥掉并给出 warning；图片按文件名顺序均匀插入。
- **图片默认预处理**：每张原图裁掉底部 `max(24px, 5% 宽度)`（660px 宽约 33px），移除 EXIF/ICC 等元数据，统一输出 JPEG quality=90 到 `images_cropped/`；docx 用裁剪图，原图 `images/` 保留不动。`--no-crop` 关闭裁剪，`--crop-bottom-px 40` 调整底部幅度，`--crop-right-px` 默认 0。
- 零图片文章也会生成纯文字 docx。
- 默认输出：`workspaces/output/<导出当天 yyyyMMdd>/<清理后的标题>.docx`；同日同名自动加 `_1`、`_2`；docx 正文不包含标题，标题只用于文件名。当天重做同一篇复用原文件名（`.docx_manifest.json` 记录）。
- 只有 docx 生成且正文首段不是标题，才会把 `state` 置 1（已采用）。失败不会标记。
- 即使脚本退出码非 0，也先读 stdout 的 JSON，把 `error` 作为失败原因记录，再继续下一篇。

**重做已采用文章时**：`select_articles.py` 和 `export_article.py` 都要加 `--force`；`crawl_article.py` 仍用 `--skip-existing` 复用缓存正文。例：

```bash
python3 skills/sohu-article-creator/scripts/select_articles.py "1" --force
python3 skills/sohu-article-creator/scripts/crawl_article.py -i workspaces/.state/selection.json --skip-existing -n 0 --root workspaces
# 重新写 rewrite.txt 后：
python3 skills/sohu-article-creator/scripts/export_article.py <ARTICLE_ID> --root workspaces --force
```

## 第 6 步：把成品上传到微云（可选）

如果用户要求把成品同步到微云（例如「上传到微云目录 workbuddy/sohu/netease_export」），
用 `scripts/upload_to_weiyun.py` **逐个文件上传**，**不要打包成 zip**——保持与本地 `output/<yyyyMMdd>` 一致的篇目目录结构。

> ⚠️ **不要直接传到 `netease_export/` 根目录**。每次上传都应在 `netease_export/` 下按上传时刻建一个
> `YYYYMMDD_HHMMSS` 时间戳子目录（如 `export/20261009_000000`），让每一批成品各自独立、
> 互不覆盖。用 `--parent <父目录> --timestamp` 即可自动生成。

```bash
# ✅ 推荐：父目录 + 自动时间戳子目录（生成 workbuddy/sohu/export/20261009_000000 这类目录）
python scripts/upload_to_weiyun.py --out ./output/20261102 --parent workbuddy/sohu/netease_export --timestamp

# 已知具体目录路径时也可直接指定（目录不存在会自动逐级创建）
python scripts/upload_to_weiyun.py --out ./output/20261102 --weiyun-path workbuddy/sohu/netease_export/20261009_000000

# 已知目标目录 dir_key 时可直接指定
python scripts/upload_to_weiyun.py --out ./output/20261102 --weiyun-dir <dir_key>
```

时间戳格式默认为 `%Y%m%d_%H%M%S`（如 `20261009_000000`），可用 `--timestamp "<格式>"` 自定义。

上传后的微云结构（与本地 `output/<yyyyMMdd>` 一一对应）


要点：

- **每批一个时间戳目录**：`--parent` + `--timestamp` 会在父目录下自动建 `YYYYMMDD_HHMMSS` 子目录，
  避免直接堆在 `netease_export/` 根下、也避免不同批次互相覆盖。
- **不加 `--zip`、不打包**：每个成品文件独立上传，微云上打开即是散文件，可直接取用。
- **路径即建即用**：`--parent` / `--weiyun-path` 从根逐级 `list`，不存在就 `create_dir`，无需事先手工建目录。
- **上传通道**：走沙箱内的 MCP 代理（`http://weiyun.agent-gateway.auth-proxy.local/api/v3/mcpserver`），
  **无需手动提供 token**；如需换地址用 `--mcp-url` 或环境变量 `WEIYUN_MCP_URL`。
- **大文件自动分片**：按 512KB 分块走「预上传 → 分片上传」，支持秒传；图片/文档都能传。


## 第 7 步：最终报告

用简洁清单汇报，不要输出分析或整篇正文：

- **成功**：标题、docx 路径、图片张数、裁剪张数/是否有裁剪回退或 warning、是否复用缓存。
- **跳过**：已采用的文章标题；说明如需重做可指定。
- **失败**：标题/ID、失败阶段（抓正文 / 重写 / 导出 / 正文标题残留检查）、原因。
- 明确说明哪些文章已在状态库标记为「已采用」。
- 不要逐篇问用户是否满意；如果用户要调整，等他们提出。
- 若有上传操作，说明上传的本地目录以及微云对应的目录。

## 常用命令速查

```bash
# 作者列表
python3 skills/sohu-article-creator/scripts/author_store.py check --json
python3 skills/sohu-article-creator/scripts/author_store.py add "<URL>" --json

# 抓最新列表（每作者 5 篇）
python3 skills/sohu-article-creator/scripts/list_latest.py -n 5 --root workspaces

# 选稿（默认跳过已采用；重做加 --force）
python3 skills/sohu-article-creator/scripts/select_articles.py "1,3,5" --json
python3 skills/sohu-article-creator/scripts/select_articles.py "1,3" --author "作者名" --json

# 抓正文（必须 -n 0）
python3 skills/sohu-article-creator/scripts/crawl_article.py \
  -i workspaces/.state/selection.json --skip-existing -n 0 --root workspaces

# 查询文章目录/标题/缓存状态
python3 skills/sohu-article-creator/scripts/state_store.py \
  --root workspaces show <ID1> <ID2> --json

# 单篇导出 docx（默认裁底部、去元数据、JPEG 90）
python3 skills/sohu-article-creator/scripts/export_article.py <ARTICLE_ID> --root workspaces

# 关闭裁剪 / 调整底部裁剪幅度（0=自动约 33px）
python3 skills/sohu-article-creator/scripts/export_article.py <ARTICLE_ID> --root workspaces --no-crop
python3 skills/sohu-article-creator/scripts/export_article.py <ARTICLE_ID> --root workspaces --crop-bottom-px 40
```

## 文件与状态

```text
workspaces/
├── .state/
│   ├── state.db                  # 文章状态；state=1 表示已采用
│   ├── latest_list.json          # 最近一次「抓一下」的编号快照
│   └── selection.json            # 本次选稿
├── articles/<作者名>_<作者ID>/<日期>_<标题>_<文章ID>/
│   ├── content.txt               # 原文正文（不要改）
│   ├── images/001.jpg ...        # 原图（不要改）
│   ├── images_cropped/001.jpg... # 裁剪后的 JPEG（export 生成，已去元数据）
│   ├── rewrite.txt               # AI 重写正文（只写正文）
│   └── rewrite.md                # 标题 + 正文 + 图片引用（docx 转换时移除标题）
└── output/
    └── <导出当天 yyyyMMdd>/
        ├── <标题>.docx            # 最终成稿（正文不含标题）
        ├── <标题>_1.docx          # 同日重名时的序号后缀
        └── .docx_manifest.json    # 当天文章→文件名映射，重做复用
```

## 边界与失败处理

- **作者主页抓取失败**：`list_latest.py` 会继续其他作者。把失败作者和原因告诉用户；不要编造文章。
- **某个作者没有文章**：正常跳过；若所有作者都没有文章，报告后停止。
- **列表为空或页面结构变化**：不要猜；报告并建议用 `--debug-dir` 排查，或确认作者 URL。
- **文章已抓取但内容文件被删**：`is_crawled` 会返回 false，显示为 `[新]` 或 `[抓取失败]`；重新选它会重新抓取。
- **正文太短/以图片为主**：尽力重写并在报告里提醒，不要编造。
- **正文标题残留校验失败**：docx 首段与标题相同会失败；检查 `rewrite.md` 是否被手工改过、状态库标题是否为空。标题只用于文件名，正文不应包含标题。
- **图片缺失**：导出会按实际存在的图片数量生成，并在 warning 里说明；不要伪造图片。
- **图片裁剪失败/太小**：`export_article.py` 会回退使用原图并在 warning 里说明，不阻断导出；不要手动改 `images_cropped/`。
- **网络波动**：脚本按作者隔离失败；串行执行，不要并发触发多个批量抓取。
- **用户只给文章 URL**：请用户提供作者主页 URL，不要自行改写作者列表。

## 交互示例

用户：`抓一下`

助手：运行 `list_latest.py`，原样展示编号列表，然后问：

> 请回复要采用的编号（如 `1,3,5` / `1-3` / `全部`）。已采用的文章默认跳过；要重做请说明。

用户：`1,3`

助手：直接执行选稿 → 抓正文 → 逐篇重写 → 逐篇导出 docx，最后给出每篇的成稿路径和状态，不再逐篇确认。

用户：`第2篇重做`

助手：用 `select_articles.py "2" --force` 选稿，复用缓存正文，重新写 `rewrite.txt`，再用 `export_article.py <ID> --force` 覆盖导出，最后汇报。

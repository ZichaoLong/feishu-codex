# Focus 安装制品交付合同

文档角色：中文规范源。英文同步副本：`docs/contracts/install-artifact-delivery.md`。

本文定义 Focus 源码、可安装 bundle、GitHub Release channel、本地构建和显式发布之间的边界。
它解决生成的 Web production assets 不再进入 Git 历史后，clone、下载与开发构建仍然共用一条可验证
安装路径的问题；它不承诺把 Python、第三方 wheel 或 Codex CLI 一并离线交付。

## 1. 术语与 owner

| 事实或动作 | 唯一 owner |
| --- | --- |
| bundle 与 channel manifest 的闭合 schema、构建和验证 | `scripts/build_support/install_bundle.py` |
| stable / development / local artifact 的选择、下载与安装事务边界 | `install.py` |
| 已安装 Python module 的隔离 argv 形状 | `bot/managed_python.py` |
| Focus wheel 的 clean build 与 source-payload 核验 | `scripts/build_support/python_distribution.py` |
| GitHub Release 状态核验、上传顺序与 development retention | `scripts/build_support/github_publication.py` |
| 本地 bundle 入口 | `scripts/build_install_bundle.py` |
| 当前 workspace 的 Web build、临时 local bundle 与正式安装器串联 | `scripts/install_workspace.sh` |
| 唯一仓库制品上传入口 | `scripts/publish_install_bundle.py` |
| 手动发布门禁 | `.github/workflows/publish-installable.yml` |
| Python 依赖声明与 lock 语义 | [Python 依赖锁决策](../decisions/python-dependency-locking.zh-CN.md) |

“bundle”是一个 ZIP 文件，不是目录，也不需要在传给 `--artifact` 前解压。源码 checkout 是构建输入，
不是安装 payload；生成的 `bot/web_assets/dist/` 与 `build/install/` 都是 ignored 本地产物。

## 2. Bundle 闭合 schema

bundle 必须恰好包含三个普通、未加密、无目录层级的 ZIP entry：

- `manifest.json`；
- 一个 Focus wheel；
- `requirements.lock`。

不允许目录、绝对路径、父目录跳转、反斜杠路径、symlink、重复 entry 或额外文件。`manifest.json`
是严格 UTF-8 JSON object，拒绝重复 key、未知字段和缺失字段。当前 schema 是：

| 字段 | 合同 |
| --- | --- |
| `schema` | 必须为 `focus-install-bundle` |
| `schema_version` | 必须为整数 `1` |
| `channel` | `stable`、`development` 或 `local` |
| `version` | Focus wheel 的非空安全版本标识 |
| `build_id` | 本次构建的非空安全标识 |
| `source_revision` | 构建声明的源码 revision；公开发布只接受 40 位小写 commit SHA，并要求内外 manifest 一致 |
| `files` | 恰好两个互不重名的 file record |

每个 file record 只允许 `name`、`role`、`size`、`sha256`。`name` 必须是不含目录的 POSIX 文件名，
`size` 必须是受限正整数，`sha256` 必须是小写 SHA-256。两个 role 必须各出现一次：

- `focus-wheel`：文件名以 `.whl` 结尾；wheel metadata 的 name 必须为 `focus`，其 version 必须与
  manifest 一致，并且必须包含 Focus Web production payload 与 notices；
- `python-dependency-lock`：文件名必须为 `requirements.lock`，内容必须是 UTF-8 locked requirements
  投影。

验证器先核对 archive 形状、展开上限、每个 payload 的 size 与 SHA-256，再只把两个声明的 payload
写入空临时目录。任何一项不一致都拒绝整个 bundle，不进入安装事务。

## 3. 外层 channel manifest

远端 `stable` 和 `development` bundle 还必须在同一个 GitHub Release 中带一个外层 channel manifest：

- stable：`focus-install-stable.json`；
- development：`focus-install-development.json`。

它同样是拒绝重复 key、未知字段和缺失字段的严格 UTF-8 JSON object：

| 字段 | 合同 |
| --- | --- |
| `schema` | 必须为 `focus-install-channel` |
| `schema_version` | 必须为整数 `1` |
| `channel` | 必须与请求的 remote channel 精确一致 |
| `release_tag` | 必须与承载它的 GitHub Release tag 精确一致 |
| `version`、`build_id`、`source_revision` | 必须与内层 bundle manifest 精确一致 |
| `bundle` | 只含 `name`、`size`、`sha256`，精确指向同一 Release 中的一个 ZIP asset |

安装器同时核对 GitHub asset metadata、下载字节数、外层 SHA-256、内外 manifest identity 和 wheel
identity。远端 Release tag 还必须解析到 `source_revision` 声明的精确 commit。外层 SHA-256 能把 channel
descriptor 绑定到同一仓库 authority 下的精确 bundle 字节，但它与
GitHub/HTTPS 不是相互独立的签名或信任根；本合同不把它描述成独立防篡改证明。

## 4. 三种安装 authority

### stable

未指定来源时，安装器等同于 `--channel stable`：读取本仓库 GitHub 的 latest 非 draft、非 prerelease
Release，并要求其中恰好存在 stable channel manifest 及其指向的 bundle。stable Release tag 去掉可选前导
`v` 后必须等于 wheel version，而且必须解析到 bundle 的 `source_revision`。stable bundle 和 stable channel
manifest 都是 immutable asset；已有同名不同内容时拒绝覆盖。

stable 发布只使用已经显式创建的正式 Release。安装器不会因为 latest Release 尚无 bundle 而回退到
development、checkout 源码或另一旧 Release。

### development

每次显式发布 development build 都创建一个独立的非 draft prerelease。其 tag 必须是
`development-build-<build_id>`，并精确解析到 bundle 的 `source_revision`；Release 页面、GitHub 自动生成的
source archives 和可安装 bundle 因而指向同一次源码快照。安装器从 GitHub Release 列表中按
`published_at` 和 Release id 选择最新发布项，并要求它是完整的 development prerelease；验证失败时不回退
到更旧 prerelease，也不读取旧的固定 `development-builds` Release。

每个 development Release 只承载该 build 唯一命名且不可覆盖的 bundle，以及不可覆盖的
`focus-install-development.json` descriptor。发布完成后 best-effort 只保留最近五个 development
prerelease；清理会同时删除整个旧 Release 及其 tag。清理失败只产生告警，不得撤销已经发布的新
prerelease。

### local artifact

`--artifact PATH` 使用用户明确选择的 bundle ZIP，不访问 GitHub，也不需要外层 channel manifest。
它仍完整验证内层 schema、所有 payload 字节、wheel identity 和 Web payload。`local` bundle 是开发者默认
构建形状；显式下载的 stable/development ZIP 也可经 `--artifact` 安装，但不会因此变成另一个 channel。

三种 authority 之间没有隐式 fallback。来源解析或验证失败时，用户修复该来源后重试或显式选择另一来源。

## 5. 安装事务与网络边界

远端 Release 查询、channel manifest 与 bundle 下载，以及本地或远端 bundle 的完整验证，都发生在 Focus
取得 offline-maintenance admission、停止 service 或修改受管 `.venv` 之前。前置阶段失败时，当前安装和
service 状态保持不变。

验证成功后，安装器才进入既有受管安装事务：核验所有实例 idle，每次删除并重建 Focus 专有的
CPython 3.11+ `.venv`，再以 bundle 内 `requirements.lock` 为 constraint 对已验证 wheel 执行 force
reinstall。pip 和安装后的一致性检查均由隔离解释器运行，且安装子进程不继承 `PYTHON*` 导入设置；系统、
Conda、用户 site-packages、当前目录和 `PYTHONPATH` 中的包不属于受管环境。安装器保留用户的 index、proxy
和证书 authority，但在写入依赖前拒绝有效的 pip `target`、`prefix`、`root` 或 `user` 配置，避免把包
安装到受管 `.venv` 之外。只有隔离的 `pip check` 通过后才刷新 wrapper、completion 与 service 定义。
四个公开 wrapper 和 completion 都由绝对受管解释器以 `-I -m <module>` 启动；service 定义直接保存同一
隔离 module argv，不再经过用户 wrapper。隔离模式只控制 Focus 当前 Python 进程的 import authority，
不会删除普通环境变量，因此 PATH、Focus/provider/Codex 配置仍可按既有合同传给下游工具。这个流程不是
热升级，也不建立多代环境或自动回滚状态机。

remote channel 需要访问 GitHub；标准 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 会由 Python 网络栈使用。
`--artifact` 只消除 Focus bundle 的 GitHub 下载，pip 仍可能按自身 index、proxy、证书和 cache 配置下载
第三方依赖。bundle 不含 Python 解释器、第三方 wheelhouse 或第三方 artifact hashes，因此用户可以先单独
下载 ZIP 再转移到目标机，但本项目不承诺完整零网络安装。

## 6. 构建与发布是两件事

开发者本地构建时，先在 `web/` 生成 production Web assets，再运行
`python scripts/build_install_bundle.py`。默认生成 `local` bundle 到 ignored 的 `build/install/`；随后通过
`bash install.sh --artifact <zip>` 或 `./install.ps1 --artifact <zip>` 安装。构建器要求当前 source 中已有
Web payload 与 `requirements.lock`，并构建、核验一个包含它们的确定性 Focus wheel。

Unix 开发者可从任意工作目录执行 `bash /path/to/focus/scripts/install_workspace.sh` 串联上述步骤。该入口不运行
`npm ci`；调用者须在首次 clone、`web/package-lock.json` 变化或 `node_modules` 缺失时先安装 Web 依赖。
它先生成 Web production assets，再在唯一临时目录构建 local bundle，要求该目录恰好产生一个 Focus ZIP，
并把该精确路径传给正式 `install.sh --artifact`。安装器返回后临时目录必须删除；任一步失败都不得继续使用
旧 bundle。该入口只接受可选的 `--migrate-from-feishu-codex` 并原样传给 `install.sh`，拒绝调用者覆盖
artifact 或 channel；它不创建另一套安装事务或发布路径。

普通 commit、pull request、CI 验证、本地 Web build 和本地 bundle build 都不发布制品。GitHub 上传必须是
显式动作：手动触发 `publish-installable.yml`，或明确调用唯一上传命令并提供已经构建、验证的 bundle 与
matching channel manifest。

发布先对 bundle、channel manifest 和 source revision 做完整 preflight。stable 再核验已存在的正式 Release
及其 tag，依次上传不可变 bundle 和 channel manifest；channel manifest 上传并读回核验成功是 stable 发布
commit point。development 则以精确 `source_revision` 为 target 创建唯一 draft prerelease，依次上传并读回
核验两个不可变 asset，最后把 draft 发布为 prerelease；只有这个可见性切换才是 development 发布 commit
point。上传或发布结果不明确时，发布器从 GitHub 读回 Release、tag 与 asset，并按 commit、size 和 SHA-256
reconciliation；无法证明相同结果就失败关闭。

正式 workflow 把已 checkout、通过门禁的 `HEAD` 写入 `source_revision`，并由 `build_id` 派生 development
tag。独立上传命令不能证明调用者 worktree clean，但发布时必须证明目标 GitHub tag 精确解析到该 commit。

stable 发布要求目标正式 Release 已存在且 assets immutable。development 每次创建唯一 draft，所有 asset
immutable，且只在完整核验后发布。普通验证不得通过复用发布脚本、workflow side effect 或隐式 tag 创建而升级
为发布。

## 7. 维护闭环

改变 schema、channel authority、安装事务边界或发布顺序时，必须在同一 transaction 中同步本合同、owner
实现、installer help、publication workflow 和 focused tests。改变 Python dependency lock 语义时同步
[Python 依赖锁决策](../decisions/python-dependency-locking.zh-CN.md)，不要在两份文档中维护竞争性说明。

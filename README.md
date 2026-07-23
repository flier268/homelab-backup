# Homelab Docker 備份

Docker Compose 服務的 Restic + OneDrive 備份工具。

- 每個 `/srv/stacks/**/backup.yaml` 自行宣告 Cron、retention、來源與一致性策略；
  可用安全的巢狀目錄分類服務，目錄位置不等於服務 ID。
- 支援 trusted root 下由容器 UID/GID 擁有的任意深層路徑，以及本機 rootful
  Docker named volume；中間 symlink 不會被跟隨，leaf symlink 只備份 link 本身。
- `backup.yaml` 會自動保存到每份 snapshot 的 `_meta/backup.yaml`，不必列入 `sources.paths`。
- 單一 systemd timer 每 5 分鐘掃描到期服務。
- 還原時可使用互動式選單，所有 repository 服務預設全選。
- 批次備份與還原會隔離各服務錯誤；單一服務失敗不會跳過後續服務，最後
  統一列出失敗摘要並回傳非零狀態。
- 備份以單份 manifest 為交易邊界；所有來源 staging 完成後才提交 Restic
  snapshot。任一來源失敗會跳過整份 manifest、清除半成品 staging，不產生
  該服務的部分備份。
- 一致性策略支援停服、hook、外部靜止、live best-effort 與 Btrfs subvolume
  snapshot；所有模式都可先執行具 timeout／required 政策的 argv action。
- 支援本機部署交叉驗證後覆寫，以及所有 target 均不存在的全新重建；混合
  狀態一律拒絕。

## 全新安裝

```bash
cd ~/Apps
git clone https://github.com/flier268/homelab-backup.git
cd homelab-backup
sudo ./install.sh
```

也可從 GitHub Releases 下載版本化的 `homelab-backup-X.Y.Z.tar.gz`，先用同一個
Release 內的 `SHA256SUMS` 驗證，再解壓並執行安裝：

```bash
sha256sum --check SHA256SUMS
tar -xzf homelab-backup-X.Y.Z.tar.gz
cd homelab-backup-X.Y.Z
sudo ./install.sh
```

維護者推送與 `homelab_backup.VERSION` 相同的 `vX.Y.Z` 標籤後，GitHub Actions
會在 CI 全數通過時自動建立 Release、產生 release notes，並附上 archive 與
checksum。一般 push 與 pull request 只執行 CI，不會發布。

安裝期間不要關機；若因斷電中止，重新執行同一個 `install.sh` 讓完整 release
重新發布。安裝器不維護跨斷電 transaction journal。

## 升級

已安裝的版本可直接升級至 GitHub Releases 的最新穩定版：

```bash
sudo backupctl upgrade
```

此指令只允許從 `/usr/local/sbin/backupctl` 的 installed layout 執行，不支援從
Git checkout 啟動。它會先下載最新 Release 的 `SHA256SUMS`，確認版本高於目前
版本後才下載對應的 `homelab-backup-X.Y.Z.tar.gz`；若已是最新版則直接成功結束，
不下載 archive，也不允許降版或安裝 prerelease。

archive 必須完整符合 Release 內的 SHA-256，且封裝只能包含預期的單一版本目錄、
一般檔案與目錄，通過路徑及 `install.sh` 驗證後才會交給既有安裝器。checksum
與 archive 都透過 GitHub HTTPS 下載；此校驗沿用 Release 的 `SHA256SUMS`，
不另外提供獨立簽章信任根。

下載、校驗、解壓或安裝任一步驟失敗時，暫存內容都會清除。既有安裝器只有在
Python runtime、鎖定依賴、Docker helper、launcher 與 systemd units 全部準備
完成後才原子切換 `current`；失敗時目前版本仍維持啟用。

接著：

1. 設定 OneDrive rclone remote。
2. 編輯 `/etc/homelab-backup/config.yaml`。
3. 建立各服務的 `backup.yaml`。
4. 執行 `sudo backupctl validate`。
5. 執行 `sudo backupctl init` 建立全新 repository。
6. 手動備份並完成還原測試。
7. 執行 `sudo ./backup-configs.sh`，貼入 SSH 公鑰並加密災難復原必要設定。
8. 最後才啟用 systemd timers。

服務 ID、顯示名稱與部署目錄彼此獨立。例如：

```text
/srv/stacks/
└── Minecraft/
    └── Advent of Ascension Plus-2026/
        └── backup.yaml
```

```yaml
version: 1
service: advent-of-ascension-plus-2026
name: "Advent of Ascension Plus-2026"
```

`service` 是用於 CLI、Restic tag 與本機 state 的穩定唯一 ID，只能包含英數、
`.`、`_`、`-`；可選的 `name` 是顯示文字，可在單字間使用單一空白。不同分類下
的 `service` 仍不得重複。快照會記錄 manifest 相對目錄；本機設定不存在時，
新快照可將 manifest 還原到原分類位置，舊快照則回退到
`/srv/stacks/<service>`。

## 解除安裝

停止並停用 timers、移除 systemd units、`backupctl`、已安裝 releases 與其
Docker helper images：

```bash
sudo ./uninstall.sh
```

預設保留 `/etc/homelab-backup`、`/var/lib/homelab-backup` 與
`/var/cache/homelab-backup`，方便稍後重新安裝。若確定連本機設定、密碼、state、
staging、restore workspace 與 cache 都不再需要，可明確要求清除：

```bash
sudo ./uninstall.sh --purge
```

兩種模式都不會移除安裝器透過 APT 安裝的共用系統套件、遠端 Restic repository，
也不會刪除 `/srv/stacks` 或 `/srv/data`。執行中的手動 `backupctl` 程序仍在使用
release 時，解除安裝會拒絕刪除程式檔。

## 災難復原必要設定

Git 保存模式只建立加密 archive：

```text
configs/homelab-backup-configs.zip.age
```

```bash
sudo ./backup-configs.sh
```

此腳本只允許 root 執行。明文只會在 `/run` 的 root 專用暫存目錄中封裝，
離開暫存目錄前會先用貼入的 `ssh-ed25519` 或 `ssh-rsa` 公鑰經 `age`
加密，並先確認 `/run` 確實位於 tmpfs。Git 模式只提交密文，另一模式會產生
timestamped 密文。

邊界：這是全新專案，只支援目前的加密 archive，不包含舊明文設定或舊 archive
遷移流程。全域設定、`backup.yaml` 與 snapshot inventory 都使用目前唯一的
`version: 1`；不處理升級或舊版 fallback。

## 支援與安全邊界

- 僅支援 Linux、root coordinator、本機 `ext4`／`xfs`／`btrfs` 與本機 rootful
  Docker。trusted root 本身可以是核准 mount 或 Btrfs subvolume，其下不允許
  其他 mount、bind mount或 nested Btrfs subvolume。
- `sources.paths[].path` 必須嚴格位於唯一的 trusted root 之下，不能等於 trusted
  root。trusted root 與其控制路徑必須是 root-owned、不可 group/world write 的
  真實目錄；其下的完整資料樹可由容器 UID/GID 擁有，並可選取其中任意深層路徑。
  程式從 trusted root 的固定 descriptor 逐層開啟來源，中間 symlink 會被拒絕；
  還原也透過固定 descriptor 寫入，不會因可寫祖先被換名或換成 symlink 而越界。
- 備份會自動在 snapshot inventory 記錄所選路徑祖先的數字 UID/GID 與 mode。
  全新 rebuild 缺少資料祖先時會依此重建，因此不會把容器資料目錄猜成
  `root:root` 或 `1000:1000`；manifest 不需增加任何欄位。沒有這項 metadata 的
  舊快照仍可還原到祖先已存在的部署，但不會在全新 rebuild 時猜測權限。
- payload 支援普通檔案、目錄、symlink、ACL、xattr 與 payload 內 hardlink；
  FIFO、socket、device node會被拒絕。`snapshot` 模式只對來源本身就是 Btrfs
  subvolume 的 path 建立唯讀 snapshot；來源內若有 nested subvolume 會拒絕，
  必須拆成獨立 source。Restic 不保存 subvolume identity、snapshot 關係、reflink、
  compression或 CoW 屬性。
- `stop`、`hooks`、`external` 不允許已知 Compose/Docker writer；`live` 與
  `snapshot` 對未受 filesystem snapshot 保護的普通 path／named volume 顯示警告，
  並在 inventory 標成 `best-effort`。唯讀 Docker mount 不視為 writer；共用 UID
  程序、排程與其他 privileged process 仍是 operator obligation。
- 多個獨立 Btrfs subvolume 會先依序建立全部 snapshot，再開始 staging；這不代表
  多個 subvolume 之間具有同一時間點的原子性。Inventory 會在每個來源記錄
  `capture_method`，並在 `consistency` 記錄實際 mode、整體 `guarantee`、staging
  前後的 optional action failures 與偵測到的 writer container IDs。
- Btrfs 暫存 snapshot 固定建立在來源所屬 trusted root 的
  `.homelab-backup-snapshots`（root-only 0700）中。建立時來源會先以 FD 釘住，
  container-owned parent 隨後被 rename／替換也不會改變 snapshot 來源。內部
  journal 以 `creating`、`ready`、`deleting` 保存完整 identity；每次備份執行
  action 前及非 dry-run maintenance 都會自動回收 crash 遺留項目，包含已停用或
  已移除服務。live source 消失或重建不會阻止舊 snapshot 精確回收。
- 所有 Compose YAML、`compose.env_file`、Restic 密碼與 rclone 設定必須是
  root-owned、不可 group/world write 的普通檔案，且不可為 symlink。Compose
  不載入隱含 `.env` 或外部 `COMPOSE_*` 設定；需要插值時必須在 manifest 明確設定：

  ```yaml
  compose:
    files: [compose.yaml]
    env_file: compose.env
  ```
- staging copy、Docker named volume copy、Restic 備份與 retention 固定以 root
  執行，以完整保存來源的數字 UID/GID、mode、ACL、xattr 與 hardlink。還原也以
  root 套用 snapshot 內的原始 metadata；完成後的資料不會一律變成 root:root，
  因此原本以指定 UID/GID 執行的容器仍可存取。
- `hooks.before` 可以建立或更新 `sources.paths` 的 payload artifact；所有 required
  Docker named volumes 必須在靜態 preflight 前已存在，不支援由 hook 動態建立。
- `actions.before[]` 與 `actions.finally[]` 都是不經 shell 的 argv；相對 executable
  會被拒絕。每個 action 都必須明確設定 `run_as`，可使用帳號名稱或加引號的
  Docker 風格 `"UID:GID"`。`run_as: root` 或 `run_as: "0:0"` 的 executable 與全部父目錄必須由
  root 擁有、不可 group/world write，且 executable 不可為 symlink。預設 timeout
  30 秒且 `required: true`。Finally 會在模式本身的重啟、hook 恢復或 snapshot
  清理之後執行，即使 before／staging 失敗也會嘗試執行。HTTP 可使用 `curl`，
  RCON 可使用既有 CLI 或 `docker compose exec`。敏感值應放在 root-only 工具設定
  或 secret，不要放入 argv。
- `actions.on_success[]` 在 Restic snapshot 成功提交後執行；
  `actions.on_failure[]` 在 staging、Restic、on-success 或 state 失敗時執行。
  Failure action 會收到 `BACKUPCTL_FAILURE_PHASE`、`BACKUPCTL_FAILURE_TYPE`、
  `BACKUPCTL_FAILURE_REASON`、`BACKUPCTL_FAILURE_SERVICE` 與
  `BACKUPCTL_FAILURE_SECONDARY`。這些環境變數不包含命令輸出
  或 argv；若 failure action 使用 `docker exec`，必須自行明確轉交需要的變數給容器。
- Snapshot 中的 Compose service 清單僅供診斷；現有部署的授權比對使用 project
  name、path/source declarations 與 logical-to-actual volume mapping。
- 不支援 remote/rootless Docker、跨 mount、ZFS、
  ext2/3、FUSE、NFS、autofs，或未經本機 manifest 授權就覆寫既有 volume。

從預設加密檔放回系統，依提示貼入 SSH 私鑰並解密：

```bash
sudo ./restore-configs.sh
```

非互動執行時需明確加上 `--yes`：

```bash
sudo ./restore-configs.sh --yes \
  /safe/path/configs.zip.age
```

換 KEY 不必重新讀取目前系統設定。輸入舊私鑰解密，接著貼入新公鑰：

```bash
sudo ./backup-configs.sh --rotate configs/homelab-backup-configs.zip.age
```

腳本會先以舊私鑰解密並驗證 archive 內容，再以新 age KEY 加密並原子替換。
私鑰只直接傳給 `age`，不放入參數、環境變數或一般磁碟。

## 手動備份與空間保護

```bash
sudo backupctl backup minecraft
sudo backupctl backup advent-of-ascension-plus-2026
```

Palworld 零停機範例可參考 `examples/palworld.backup.yaml`。若其中宣告的資料 path
本身是 Btrfs subvolume，`snapshot` 會從唯讀 filesystem snapshot staging；若只是
普通目錄，仍會直接 staging，偵測到 writer 時整份備份會標為 `best-effort`。

備份會在來源靜止後保守估算 path 與 Docker volume 的 staging 大小，並要求
staging filesystem 至少保留 1 GiB。空間不足時預設中止；只有人工操作可明確
放行，排程 `run-due` 不會自動略過保護：

```bash
sudo backupctl backup minecraft --allow-low-space
```

## 還原全部服務

互動選取，預設全選：

```bash
sudo backupctl restore --apply --start
```

非互動式全部還原：

```bash
sudo backupctl restore --all \
  --restore-manifest --apply --start --yes
```

查詢指定服務的 snapshot：

```bash
sudo backupctl snapshots minecraft
```

指定服務的 snapshot 清單會追加唯讀 retention preview，顯示每份 snapshot
目前符合的保留原因（例如 last、daily、weekly）以及 policy 將移除的項目；
preview 不會執行 forget 或 prune。

指定歷史 snapshot ID 時只允許單一服務；程式會先確認該 ID 屬於目前
host 與指定的 service tag，再開始下載：

```bash
sudo backupctl restore minecraft --snapshot 01234567 --apply
```

刪除指定 snapshot 時也會先驗證目前 host 與 service tag。互動模式會再次
確認；非互動模式必須指定 `--yes`。預設只移除 snapshot metadata，未被引用的
repository 資料會在下次 maintenance 回收；需要立即回收可加 `--prune`：

```bash
sudo backupctl delete-snapshot minecraft 01234567
sudo backupctl delete-snapshot minecraft 01234567 --prune --yes
```

安全政策：只要 stdin 不是 TTY，所有 `restore`（包含只下載、不加
`--apply`）都必須明確指定 `--yes`。

下載每個 service 前，程式會以 Restic 的 `restore-size` 估算 snapshot 展開後的
大小，並確認 `restore_root` 所在檔案系統完成下載後至少還有 1 GiB 可用空間。
空間不足時會顯示估算值與缺口：互動模式可再次確認後繼續；非互動模式必須另外
明確指定 `--allow-low-space`。`--yes` 本身不會略過磁碟空間保護。

`restore --apply` 成功後會自動刪除該次下載的暫存副本；套用失敗或單純下載
則保留，方便檢查或稍後手動套用。手動刪除指定副本或批量刪除全部副本：

```bash
sudo backupctl cleanup-restores minecraft/20260717-120000-000000001 --yes
sudo backupctl cleanup-restores \
  minecraft/20260717-120000-000000001 \
  ghost/20260717-120500-000000002 --yes
sudo backupctl cleanup-restores --all --yes
```

全新重建若在發布 Compose／manifest 前失敗，會嘗試移除本次新建的
path 與 Docker volume，讓相同還原可安全重跑；任何 rollback 失敗會保留
原始錯誤並另外列出 cleanup 錯誤。若 path 在發布後被其他程序修改，或本次
建立的祖先目錄已出現其他內容，rollback 會保留該路徑並警告，不會把外部
寫入視為本次還原所擁有的資料。

安裝、設定還原與資料還原期間均不要關機。一般錯誤會在目前程序內回滾；
斷電不在原子性保證內。重新開機後可重新執行相同命令；全新重建若留下
partial target，先依錯誤訊息確認並移除該 path／volume，再重跑。

完整說明請開啟 `GUIDE.html`。

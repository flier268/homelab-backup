# Homelab Docker 備份

Docker Compose 服務的 Restic + OneDrive 備份工具。

- 每個 `/srv/stacks/**/backup.yaml` 自行宣告 Cron、retention、來源與一致性策略；
  可用安全的巢狀目錄分類服務，目錄位置不等於服務 ID。
- 支援由受保護 manifest 精確授權、可由容器 UID/GID 擁有的任意深層路徑，以及本機 rootful
  Docker named volume；中間 symlink 不會被跟隨，leaf symlink 只備份 link 本身。
- `backup.yaml` 會自動保存到每份 snapshot 的 `_meta/backup.yaml`，不必列入 `sources.paths`。
- 單一 systemd timer 每 5 分鐘掃描到期服務。
- 還原時可使用互動式選單，所有 repository 服務預設全選。
- 批次備份與還原會隔離各服務錯誤；單一服務失敗不會跳過後續服務，最後
  統一列出失敗摘要並回傳非零狀態。
- 備份以單份 manifest 為交易邊界；所有來源 staging 完成後才提交 Restic
  snapshot。任一來源失敗會跳過整份 manifest、清除半成品 staging，不產生
  該服務的部分備份；Restic 成功提交後也會立即清除 staging，不在本機保留
  完整副本。
- 一致性策略支援停服、hook、外部靜止、live best-effort 與 Btrfs subvolume
  snapshot；所有模式都可先執行具 timeout／required 政策的 argv action。
- 現有部署會以受保護的本機 manifest 交叉驗證後覆寫；全新重建則先下載、
  審核並安裝 manifest，再由同一份本機授權套用。混合狀態一律拒絕。

## 設定檔編輯器

專案提供 Vue 3 + Element Plus 的離線設定檔編輯器，可建立或匯入全域
`config.yaml` 與服務 `backup.yaml`。服務編輯器包含 Cron、retention、五種
一致性模式、path／volume sources，以及統一的 backup／restore 五階段
actions、inline Bash 與安全說明。

編輯器會產生一段可在服務根目錄執行的 `python3` 清單命令。命令輸出版本化
JSON Lines，不依賴 `ls` 的語系格式，並可安全表示空白、Unicode、tab 與換行；
將結果貼入或匯入編輯器後，即可把檔案標成備份、排除或不納入，再整理成多個
source 群組。清單不含檔案內容，也不會由瀏覽器上傳。

Release archive 根目錄包含可直接雙擊開啟的 `GUIDE.html`。
指南與編輯器由同一套 Vue 原始碼編譯成單一離線頁面；預設開啟設定編輯器，
可在頁面內切換完整指南，編輯器各區也會連到對應說明章節。本機開發與產生
離線 HTML：

```bash
cd docs
pnpm install
pnpm run dev
pnpm run check
```

建置會產生 repository 根目錄的 `GUIDE.html`，並保留
`docs/dist/GUIDE.html`。兩者皆已加入 `.gitignore`，不由 Git 追蹤。
編輯器只做靜態 schema 驗證；部署設定後仍必須在目標主機執行：

```bash
sudo backupctl validate
```

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

升版時只需修改 `homelab_backup/__init__.py` 的 `VERSION`，不必全域取代測試
內容。提交並確認 CI 通過後，建立完全相同版本的 `vX.Y.Z` tag 並推送；
Release workflow 會拒絕 tag 與 `VERSION` 不一致的發布。

安裝期間不要關機；若因斷電中止，重新執行同一個 `install.sh` 讓完整 release
重新發布。安裝器不維護跨斷電 transaction journal。

## 升級

已安裝的版本可直接升級至 GitHub Releases 的最新穩定版：

```bash
sudo backupctl upgrade
```

升級後可由桌面使用者開啟目前版本隨附的離線指南與設定檔編輯器：

```bash
backupctl docs
```

`upgrade` 指令只允許從 `/usr/local/sbin/backupctl` 的 installed layout 執行，
不支援從 Git checkout 啟動。它會先下載最新 Release 的 `SHA256SUMS`，確認版本高於目前
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
的 `service` 仍不得重複。快照會記錄 manifest 相對目錄供人工重建時參考；
repository 內的 manifest 本身不會取得 root 寫入授權。

備份完成 staging 後會比較上次成功快照的內容指紋；內容未變時預設不建立新的
Restic snapshot。若某個服務需要每次排程都留下時間點版本，可在
`backup.yaml` 設定 `create_snapshot_if_unchanged: true`。

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

- 僅支援 Linux；path source 可位於 Linux 可存取、包含 NTFS 在內的 filesystem。Compose／volume 模式需要
  本機 rootful Docker；`files` 純檔案模式不需要 Docker。
- 受 root 保護的本機 `backup.yaml` 授權其中宣告的確切 path；控制路徑不可經過
  symlink，資料來源以固定 descriptor 開啟且拒絕 nested mount。資料樹可由容器
  UID/GID 擁有。
- NTFS 能保存的 UID/GID、mode、ACL、xattr、symlink 與 hardlink 語意取決於驅動
  與掛載選項，不保證能完整重建 POSIX metadata。
- 支援普通檔案、目錄、symlink、ACL、xattr 與 payload 內 hardlink；拒絕 FIFO、
  socket 與 device node。還原會保留原始數字 UID/GID 與 mode。
- `stop`、`hooks`、`external` 會拒絕已知 Docker writer；`live`、`files`
  與未完整受 Btrfs snapshot 保護的來源只提供 `best-effort` 一致性。
- 不支援 remote/rootless Docker 與來源內 nested mount。NFS、autofs、FUSE、ZFS
  等非 Btrfs filesystem 可作一般 file-level、best-effort path source；斷線或
  automount 尚未就緒時不保證可用，metadata 與 POSIX 語意取決於 filesystem
  及 mount options，且不提供 snapshot consistency。
- 所有 Compose YAML、`compose.env_file`、Restic 密碼與 rclone 設定必須是
  root-owned、不可 group/world write 的普通檔案，且不可為 symlink。Compose
  不載入隱含 `.env` 或外部 `COMPOSE_*` 設定；需要插值時必須在 manifest 明確設定：

  ```yaml
  compose:
    files: [compose.yaml]
    env_file: compose.env
  ```
- Actions 依 `actions.backup`／`actions.restore` 分組；每項必須擇一使用不經
  shell 的 `command` argv，或由 `/bin/bash -euo pipefail -c` 執行的 inline
  `script`，並指定 `run_as`。密碼或 token 不可放入 argv。
  完整欄位、writer 判定與 Btrfs 限制請見 `GUIDE.html`。

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

非 Docker 服務或一般主機檔案可使用明確的純檔案模式；完整範例見
`examples/files.backup.yaml`：

```yaml
consistency:
  mode: files

actions:
  backup:
    before:
      - name: quiesce-service
        command: [/usr/local/sbin/example-backup-quiesce]
        timeout: 60
        required: true
        run_as: root
    finally:
      - name: resume-service
        command: [/usr/local/sbin/example-backup-resume]
        timeout: 60
        required: true
        run_as: root

sources:
  paths:
    - id: data
      path: /srv/example
  volumes: []
```

`files` 禁止 `compose` 與 Docker volume，不執行 Docker writer 偵測。Actions
仍依各自的 backup／restore 生命週期執行，但 inventory 一律保守標示為
`best-effort`。Files mode 不啟停外部服務且不可搭配 `--start`；若 actions
協調外部服務，命令必須自行保存並恢復原始狀態。
安裝程序仍會使用 Docker 建置 volume helper image；完成安裝後，只有 `files`
manifests 的備份、驗證、排程與還原不需要 Docker daemon。

備份會在來源靜止後保守估算 path 與 Docker volume 的 staging 大小，並要求
staging filesystem 至少保留 1 GiB。空間不足時預設中止；只有人工操作可明確
放行，排程 `run-due` 不會自動略過保護：

```bash
sudo backupctl backup minecraft --allow-low-space
```

## 還原全部服務

現有部署已有受保護的本機 manifest 時，可互動選取並直接套用：

```bash
sudo backupctl restore --apply --start
```

非互動式套用所有既有服務：

```bash
sudo backupctl restore --all \
  --restore-manifest --apply --start --yes
```

安全政策：只要 stdin 不是 TTY，所有 `restore`（包含只下載、不加
`--apply`）都必須明確指定 `--yes`。

還原前會列出完整 plan，包含每個 path target、Docker volume 與
existing/rebuild mode。`--yes` 只確認執行，不授權新路徑，也不會略過空間保護。
現有部署必須通過本機 manifest 交叉驗證；snapshot manifest 不能改寫目標。

只有實際 `restore --apply` 或 `backupctl apply` 會執行
`actions.restore`；只下載 snapshot 不執行。固定順序為
`before → 還原資料 → after → finally → 恢復服務 → on_success`，失敗則在
`finally` 後執行 `on_failure`，再停止 action 額外啟動的 containers 並完成
rollback／服務狀態協調。資料已寫回後若 required action 失敗，Compose 應用
維持停止，避免以半還原狀態重新接受寫入。

PostgreSQL 不停機 logical dump 與自動 `pg_restore` 的完整設定見
`examples/postgres.backup.yaml`。

全新服務必須使用兩階段流程。先只下載，審核 `_meta/backup.yaml`，再將它以
root-only 權限安裝到 `services_root` 下的預定服務目錄：

```bash
sudo backupctl restore minecraft

sudo install -d -o root -g root -m 0755 /srv/stacks/minecraft
sudo install -o root -g root -m 0600 \
  /var/lib/homelab-backup/restores/minecraft/RESTORE_ID/_meta/backup.yaml \
  /srv/stacks/minecraft/backup.yaml

sudo backupctl validate
sudo backupctl apply minecraft \
  /var/lib/homelab-backup/restores/minecraft/RESTORE_ID \
  --start --yes
```

若 `_meta/inventory.json` 中有 `present: true` 的 Compose volume，請核對其
`actual_name` 與還原的 Compose 設定。套用時會先發布已驗證的 Compose files，
再以 `docker compose config --format json` 解析實際名稱、`driver`、
`driver_opts` 與 labels。工具只執行 `docker volume create` 建立需要還原的
named volume，補上 Compose identity 與本次操作 ownership labels，驗證結果
與 snapshot inventory 一致後才還原資料；這個階段不建立 image、container
或 network。

`external: true` 的 volume 不由 Compose 建立，而且無法納入 rebuild 的安全
rollback，因此不支援由這個流程直接覆寫；請將它視為獨立管理的 exact `name`
來源，審核其還原與失敗復原方式後另行處理。

請以實際輸出的 restore ID 與審核後選定的服務目錄取代範例值。只有部分 target
存在的混合狀態一律拒絕。歷史 snapshot、刪除與 restore workspace 清理方式請見
`GUIDE.html`。

完整說明請開啟 `GUIDE.html`。

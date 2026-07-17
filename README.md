# Homelab Docker 備份

Docker Compose 服務的 Restic + OneDrive 備份工具。

- 每個 `/srv/stacks/<service>/backup.yaml` 自行宣告 Cron、retention、來源與一致性策略。
- 支援 root-owned control parent 下的完整 managed leaf，以及本機 rootful
  Docker named volume；路徑中的 symlink 只備份 link 本身。
- `backup.yaml` 會自動保存到每份 snapshot 的 `_meta/backup.yaml`，不必列入 `sources.paths`。
- 單一 systemd timer 每 5 分鐘掃描到期服務。
- 還原時可使用互動式選單，所有 repository 服務預設全選。
- 支援本機部署交叉驗證後覆寫，以及所有 target 均不存在的全新重建；混合
  狀態一律拒絕。

## 全新安裝

```bash
cd ~/Apps
git clone https://github.com/flier268/homelab-backup.git
cd homelab-backup
sudo ./install.sh
```

接著：

1. 設定 OneDrive rclone remote。
2. 編輯 `/etc/homelab-backup/config.yaml`。
3. 建立各服務的 `backup.yaml`。
4. 執行 `sudo backupctl validate`。
5. 執行 `sudo backupctl init` 建立全新 repository。
6. 手動備份並完成還原測試。
7. 執行 `sudo ./backup-configs.sh`，貼入 SSH 公鑰並加密災難復原必要設定。
8. 最後才啟用 systemd timers。

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

- 僅支援 Linux、root 執行、本機 `ext4`／`xfs`／`btrfs` 與本機 rootful
  Docker。trusted root 本身可以是核准 mount 或 Btrfs subvolume，其下不允許
  其他 mount、bind mount或 nested Btrfs subvolume。
- `sources.paths[].path` 必須是完整 managed leaf。從 `/` 到 leaf parent 都必須
  是 root-owned、不可 group/world write 的真實目錄；leaf 目錄內可由容器 UID/GID
  擁有。leaf 必須嚴格位於 trusted root 之下，不能等於 trusted root；不要把
  容器可寫資料樹中的深層檔案另列為 target。
- payload 支援普通檔案、目錄、symlink、ACL、xattr 與 payload 內 hardlink；
  FIFO、socket、device node會被拒絕。不保存 Btrfs subvolume identity、snapshot
  關係、reflink、compression或 CoW 屬性。
- 操作期間不得存在未受控 writer。程式會停止及檢查已知 Compose/Docker writer，
  唯讀 Docker mount 不視為 writer；共用 UID 程序、排程與其他 privileged process
  仍是 operator obligation。
- `hooks.before` 可以建立或更新 `sources.paths` 的 payload artifact；所有 required
  Docker named volumes 必須在靜態 preflight 前已存在，不支援由 hook 動態建立。
- Snapshot 中的 Compose service 清單僅供診斷；現有部署的授權比對使用 project
  name、path/source declarations 與 logical-to-actual volume mapping。
- 不支援 remote/rootless Docker、任意深層 restore target、跨 mount、ZFS、
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

安全政策：只要 stdin 不是 TTY，所有 `restore`（包含只下載、不加
`--apply`）都必須明確指定 `--yes`。

完整說明請開啟 `GUIDE.html`。

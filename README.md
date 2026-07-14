# Homelab Docker 備份

Docker Compose 服務的 Restic + OneDrive 備份工具。

- 每個 `/srv/stacks/<service>/backup.yaml` 自行宣告 Cron、retention、來源與一致性策略。
- 支援 bind mount 與 Docker named volume。
- `backup.yaml` 會自動保存到每份 snapshot 的 `_meta/backup.yaml`，不必列入 `sources.paths`。
- 單一 systemd timer 每 5 分鐘掃描到期服務。
- 還原時可使用互動式選單，所有 repository 服務預設全選。
- 本機缺少 `backup.yaml` 時，直接使用 snapshot 中的 manifest。

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
7. 執行 `./backup-configs.sh` 保存災難復原必要設定。
8. 最後才啟用 systemd timers。

## 災難復原必要設定

專案中的 `configs/` 應包含：

```text
configs/restic-password
configs/rclone.conf
configs/config.yaml
```

備份這三個檔案：

```bash
sudo ./backup-configs.sh
```

此腳本只允許 root 執行，因為輸出內容包含 Restic 密碼與 rclone 帳戶憑證。
選擇 Git 保存時，腳本會先顯示 configured remotes；確認所有目的地都是嚴格控管的 private repository 後，必須輸入完整的 `PRIVATE`，腳本才會提交設定並詢問是否 push。

從 `configs/` 放回系統：

```bash
./restore-configs.sh
```

非互動環境必須明確確認覆寫：

```bash
./restore-configs.sh --yes
```

如果任何檔案缺失，還原腳本會列出缺失項目並終止。

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

完整說明請開啟 `GUIDE.html`。

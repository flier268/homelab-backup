<template>
  <section id="global-config">
    <h2>8. 設定本機 repository</h2>
    <pre v-pre><code>sudo editor /etc/homelab-backup/config.yaml</code></pre>
    <h3>第一台主機</h3>
    <pre v-pre><code>version: 1
host_id: server-a
services_root: /srv/stacks
trusted_data_roots:
  - /srv/stacks
  - /srv/data

repository: rclone:onedrive:Backups/restic/server-a
password_file: /etc/homelab-backup/restic-password
rclone_config: /etc/homelab-backup/rclone/rclone.conf

staging_root: /var/lib/homelab-backup/staging
restore_root: /var/lib/homelab-backup/restores
state_root: /var/lib/homelab-backup/state
cache_root: /var/cache/homelab-backup/restic

lock_file: /run/homelab-backup/backupctl.lock
volume_helper_image: homelab/volume-rsync:1

rclone:
  bwlimit: ""

check:
  read_data_subset: 5%</code></pre>
    <p>全域設定只接受文件列出的欄位；錯誤型別會在載入時停止，不會延後到正式備份。</p>
    <h3>第二台主機</h3>
    <pre v-pre><code>host_id: server-b
repository: rclone:onedrive:Backups/restic/server-b</code></pre>
    <div class="box">主機可共用 OneDrive 帳號，但應使用不同 Restic repository，隔離 lock、prune、check 與故障範圍。</div>
  </section>

  <section id="config-backup">
    <h2>9. 備份災難復原必要設定</h2>
    <pre v-pre><code>cd ~/Apps/homelab-backup
sudo ./backup-configs.sh</code></pre>
    <p>
      腳本只允許 root 執行，並在 <code>/run</code> 的 root 專用 tmpfs 目錄封裝三個必要設定；
      離開暫存目錄前會以貼入的 SSH 公鑰透過 <code>age</code> 加密。
    </p>
    <ol>
      <li><strong>Git：</strong>建立並提交 <code>configs/homelab-backup-configs.zip.age</code>，確認 private remote 後才推送。</li>
      <li><strong>加密檔：</strong>建立 timestamped <code>.zip.age</code>。</li>
    </ol>
    <div class="box warn">私鑰必須另外保存在 Bitwarden 或離線安全媒體；沒有對應私鑰就無法還原。</div>
    <h3>解密並放回設定</h3>
    <pre v-pre><code>sudo ./restore-configs.sh</code></pre>
    <p>還原前會驗證 archive 恰好包含 Restic 密碼、rclone 設定與全域設定，三者會整組安裝或整組回滾。</p>
    <h3>更換 SSH KEY</h3>
    <pre v-pre><code>sudo ./backup-configs.sh --rotate configs/homelab-backup-configs.zip.age</code></pre>
    <p>只有完整解密、內容驗證與重新加密成功後才會原子替換舊檔。</p>
  </section>

  <section id="manifest">
    <h2>10. 每個服務的 <code>backup.yaml</code></h2>
    <div class="box ok"><code>backup.yaml</code> 會自動保存到 snapshot 的 <code>_meta/backup.yaml</code>，不必加入 <code>sources.paths</code>。</div>
    <table>
      <thead><tr><th>項目</th><th>用途與規則</th></tr></thead>
      <tbody>
        <tr><td><code>service</code></td><td>穩定唯一 ID，用於 CLI、tag、state、staging 與 restore；不可含空白。</td></tr>
        <tr><td><code>name</code></td><td>可選顯示名稱，不影響備份身分。</td></tr>
        <tr><td><code>enabled</code></td><td>控制是否參與排程；停用的 manifest 仍參與 ID 重名檢查。</td></tr>
        <tr><td><code>create_snapshot_if_unchanged</code></td><td>內容未變時是否仍強制建立時間點 snapshot。</td></tr>
      </tbody>
    </table>
    <pre v-pre><code>version: 1
service: advent-of-ascension-plus-2026
name: "Advent of Ascension Plus-2026"
enabled: true
create_snapshot_if_unchanged: false

schedule:
  cron: "0 2,8,14,20 * * *"
  retry_after: 30m
  max_lateness: 2h

retention:
  keep_last: 8
  keep_daily: 14
  keep_weekly: 8
  keep_monthly: 12
  keep_yearly: 3

compose:
  files: [compose.yaml]
  env_file: .env

consistency:
  mode: stop
  timeout: 120
  before: []
  after: []

actions:
  before: []
  finally: []
  on_success: []
  on_failure: []

sources:
  paths:
    - id: compose
      path: compose.yaml
    - id: env
      path: .env
      required: false
    - id: data
      path: ./data
      include: [world/**, config/**, mods/**, server.properties]
      exclude: [mods/cache/**]
  volumes: []</code></pre>
    <div class="box warn">需要環境插值時必須明確設定 <code>compose.env_file</code>；未設定時使用空環境檔。</div>
    <p>即使目錄含空白，CLI 仍使用穩定 ID：<code>sudo backupctl backup advent-of-ascension-plus-2026</code>。</p>
  </section>
</template>

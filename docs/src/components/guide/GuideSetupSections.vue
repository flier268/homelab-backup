<template>
  <section id="goal">
    <h2>1. 目標與架構</h2>
    <div class="box ok">
      <strong>服務仍由 Docker Compose 管理。</strong>
      每個服務在自己的目錄放一份 <code>backup.yaml</code>，宣告穩定服務 ID、排程、保留策略、來源與一致性方式。
    </div>
    <div class="flow">服務目錄中的 backup.yaml
↓ backupctl 每 5 分鐘遞迴掃描
停止服務或執行 hook
↓
本機 staging
↓
Restic 加密、去重、快照
↓
rclone → OneDrive repository</div>
    <table>
      <thead><tr><th>元件</th><th>用途</th></tr></thead>
      <tbody>
        <tr><td>Docker Compose</td><td>執行 Minecraft、Palworld、Ghost 等服務。</td></tr>
        <tr><td><code>backup.yaml</code></td><td>每個服務自己的排程、保留策略與來源清單。</td></tr>
        <tr><td><code>backupctl</code></td><td>掃描 manifest、建立 staging、控制 Compose、執行 Restic。</td></tr>
        <tr><td>Restic</td><td>加密、去重、snapshot、forget、prune、check、restore。</td></tr>
        <tr><td>rclone</td><td>讓 Restic 使用 OneDrive 作為 repository backend。</td></tr>
        <tr><td>systemd timer</td><td>每 5 分鐘檢查到期工作，並週期性維護 repository。</td></tr>
      </tbody>
    </table>
  </section>

  <section id="editor">
    <h2>設定檔編輯器</h2>
    <p>
      Release archive 根目錄包含 <code>GUIDE.html</code>。
      指南與編輯器由同一套 Vue 元件編譯，可在單一頁面直接切換且全程離線。
    </p>
    <ul>
      <li>新增或匯入全域 <code>config.yaml</code> 與服務 <code>backup.yaml</code>。</li>
      <li>編輯排程、保留策略、來源、五種一致性模式、hooks 與四階段 actions。</li>
      <li>匯入清單命令產生的 JSON Lines，將項目標成備份、排除或不納入，再整理成 source 群組。</li>
      <li>清單只有相對路徑及類型，不讀取檔案內容，也不跟隨 symlink。</li>
    </ul>
    <div class="box warn">
      瀏覽器無法檢查主機 ownership、filesystem、Compose 插值、Docker volume 或 executable。
      下載 YAML 後仍須執行 <code>sudo backupctl validate</code>。
    </div>
    <h3>本機開發與建置</h3>
    <pre v-pre><code>cd docs
pnpm install
pnpm run dev
pnpm run check</code></pre>
    <p>
      離線產物會寫入 repository 根目錄的 <code>GUIDE.html</code>，並保留一份於
      <code>docs/dist/GUIDE.html</code>；兩者皆為不納入 Git 的編譯產物。
    </p>
  </section>

  <section id="requirements">
    <h2>2. 前置條件</h2>
    <ul>
      <li>Debian、Ubuntu 或 Linux Mint 類型系統。</li>
      <li>目前登入帳號具備 <code>sudo</code> 權限。</li>
      <li>兩台主機需要各自執行本手冊。</li>
      <li>Microsoft OneDrive 帳號可正常登入。</li>
      <li>服務可以短暫停止，或能透過 hook 產生一致性快照。</li>
    </ul>
    <pre v-pre><code>cat /etc/os-release
uname -a
id
sudo -v</code></pre>
  </section>

  <section id="docker">
    <h2>3. 安裝或確認 Docker Compose</h2>
    <p>先檢查：</p>
    <pre v-pre><code>docker --version
docker compose version</code></pre>
    <p>若兩者都能正常輸出，可以直接前往下一章。Ubuntu／Linux Mint 可安裝 Docker 官方套件：</p>
    <pre v-pre><code>sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu \
${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
| sudo tee /etc/apt/sources.list.d/docker.list &gt; /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker</code></pre>
    <h3>讓目前帳號可操作 Docker</h3>
    <pre v-pre><code>sudo usermod -aG docker "$USER"</code></pre>
    <p>登出後重新登入，再執行 <code>docker run --rm hello-world</code> 測試。</p>
    <div class="box warn">加入 <code>docker</code> 群組實質上等同授予高權限，不應加入不受信任的帳號。</div>
  </section>

  <section id="directories">
    <h2>4. 建立 Compose 服務目錄</h2>
    <p>Compose 專案統一放在 <code>services_root</code>，可依用途使用多層分類目錄：</p>
    <pre v-pre><code>/srv/stacks/&lt;分類&gt;/&lt;顯示用目錄名稱&gt;</code></pre>
    <p>manifest、Compose files 與每層父目錄都不可由一般使用者寫入；路徑含空白時要加引號：</p>
    <pre v-pre><code>sudo install -d -o root -g root -m 755 \
  /srv/stacks \
  "/srv/stacks/Minecraft" \
  "/srv/stacks/Minecraft/Advent of Ascension Plus-2026"
sudo chmod 755 /srv /srv/stacks
sudoedit "/srv/stacks/Minecraft/Advent of Ascension Plus-2026/backup.yaml"</code></pre>
    <div class="box warn">
      <code>backup.yaml</code> 可宣告以 root 執行的 hooks，因此控制目錄與設定檔不可
      group/world writable。容器資料子樹可由容器 UID/GID 擁有，不要遞迴 chown。
    </div>
    <pre v-pre><code>/srv/stacks/
├── Minecraft/
│   └── Advent of Ascension Plus-2026/
│       ├── compose.yaml
│       ├── .env
│       ├── backup.yaml
│       └── data/
└── Web/
    └── ghost/
        ├── compose.yaml
        ├── backup.yaml
        └── content/</code></pre>
    <div class="box">
      Compose 不會自動載入隱含 <code>.env</code>。需要環境插值時，必須在
      <code>compose.env_file</code> 明確指定；控制檔不可為 symlink。
    </div>
  </section>

  <section id="git">
    <h2>5. 可選：用 Git 保存 <code>/srv/stacks</code></h2>
    <p>
      可以把 Compose、Dockerfile、Caddyfile 與非機密 scripts 放入 private Git。
      這是版本管理的加分項，不是災難復原必要條件。
    </p>
    <div class="box warn">不要把公開 Git 當成 secrets 儲存位置；<code>.env</code>、Restic 密碼與 rclone token 不應公開。</div>
  </section>

  <section id="install">
    <h2>6. 從 GitHub 取得並安裝專案</h2>
    <pre v-pre><code>sudo apt update
sudo apt install -y git
cd ~/Apps
git clone https://github.com/flier268/homelab-backup.git
cd homelab-backup
sudo ./install.sh</code></pre>
    <p>安裝器會建立版本化 release、Python venv、helper image、全域路徑與 systemd units，但不會立即啟用 timer。</p>
    <div class="box warn">安裝期間不要關機；若因斷電中止，重新執行 <code>sudo ./install.sh</code>。</div>
    <h3>解除安裝</h3>
    <pre v-pre><code>sudo ./uninstall.sh
# 確定連本機設定、state、staging 與 cache 都不要時：
sudo ./uninstall.sh --purge</code></pre>
    <div class="box">解除安裝不會刪除遠端 Restic repository、<code>/srv/stacks</code> 或 <code>/srv/data</code>。</div>
  </section>

  <section id="onedrive">
    <h2>7. 設定 OneDrive</h2>
    <pre v-pre><code>sudo rclone config \
  --config /etc/homelab-backup/rclone/rclone.conf</code></pre>
    <ol>
      <li>選擇 <code>n</code> 建立 remote，名稱固定為 <code>onedrive</code>。</li>
      <li>Storage 選 Microsoft OneDrive，依提示完成 OAuth。</li>
      <li>選擇正確的 OneDrive drive。</li>
    </ol>
    <pre v-pre><code>sudo chmod 600 /etc/homelab-backup/rclone/rclone.conf
sudo rclone lsd onedrive: \
  --config /etc/homelab-backup/rclone/rclone.conf</code></pre>
    <p>本方案不使用 mount；Restic 直接使用 <code>rclone:onedrive:Backups/restic/server-a</code>。</p>
  </section>
</template>

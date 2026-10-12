# agent-stream：讓 AI 實況主「阿黃」玩中世紀守塔

這個資料夾是給直播平台（`C:\Users\a5816\Claude\Youtuber\agent-live-steaming-game`）用的 **game MCP server**。
它用 Playwright 開一個 Microsoft Edge app 視窗載入 `../game.html`，再透過遊戲本來就有的全域函式和 `OllamaAI` 輔助函式來操作，**不會修改 `game.html`**。

- 工具：`observe`、`choose_level`、`build`、`upgrade`、`sell`、`start_wave`、`use_skill`、`set_speed`、`watch`、`retry`、`back_to_levels`。
- 關卡星數和寶石存在遊戲的 localStorage 裡，所以用固定的瀏覽器設定檔 `profile/`，下次開台還在。
- 直播主用麥克風講話時，`watch` 會提早回傳 `viewer_spoke`（`stream_interrupt.py` 會去問直播平台）。
- OBS 擷取的視窗標題是「中世紀守塔 - Kingdom Defense」。

## 安裝與測試

```bash
uv sync
```

```bash
uv run pytest -q
```

測試會用無頭（看不到視窗）的 Edge，加上一個臨時的瀏覽器設定檔。

## 開台

在直播平台的專案裡執行：

```bash
uv run python -m agent_stream.launch medieval-tower-defense
```

## 環境變數

| 變數 | 說明 |
|---|---|
| `TD_HEADLESS=1` | 不顯示視窗（測試用） |
| `TD_PROFILE` | 瀏覽器設定檔的位置（預設 `profile/`） |
| `TD_GAME` | 遊戲檔案的路徑（預設 `../game.html`） |

---
name: world-forge
description: words2world 的「鍛造」階段（GPU 階段）：讀場景描述或建造階段留下的待辦，生成空間概念圖、拆出物件清單（含變體）、用 ComfyUI 迭代 2D 圖、去背、TRELLIS 1024 轉 3D。執行期間 Blender 與遊戲引擎必須關閉。使用時機：開始一個新世界（文字或圖片描述 → 物件），或建造階段交接過來「這些物件需要生成」。
---

# world-forge：生成物件（GPU 階段）

你負責把「需要的東西」變成 3D 模型。這個階段**不開 Blender、不開遊戲引擎**：TRELLIS 用 1024 模型時需要整張 12 GB 顯卡，任何其他佔用都會讓它 OOM。ComfyUI 與 TRELLIS 也不能同時跑：先做完所有 2D，停掉 ComfyUI，再去背、再 TRELLIS。

所有指令在 repo 根目錄用 Windows Python 執行：`python w2w.py <command> <world> ...`（`python w2w.py` 列出全部）。
判斷標準在 [docs/ART_RULES.md](../../../docs/ART_RULES.md)，格式在 [docs/FORMATS.md](../../../docs/FORMATS.md)：開始前讀第 1–3 節與第 7 節。

## 0. 開始前

1. `python w2w.py status <world>`（新世界則跳到第 1 步）。如果是接手，讀交接訊息、`plan/spaces/*.json` 的 `notes_for_next`、最新的 `shots/r*/`。
2. 確認 Unreal / Blender 沒有在跑（`w2w.py matte` / `trellis` 也會拒絕執行）；Git Bash 跑 `df -h /c /d` 確認磁碟空間（每個 1024 模型約 20–60 MB）。
3. 不要在 `trellis2` conda 環境安裝或升級任何套件；不要印出 `~/.cache/huggingface/token`；FLUX 只用 klein 4B。

## 1. 規劃（只有新世界）

1. `python w2w.py new <world> --brief <描述檔>`；使用者給的圖放 `worlds/<world>/refs/`。
2. 讀完整描述，填 `plan/world.json` 的 `style`（look / palette / materials / avoid）。所有 prompt 都要共用這套語彙。
3. 把世界切成**小空間**：一個房間、一面外牆、門廊、一個角落、一段小徑、林間空地。每個寫一個 `plan/spaces/<space>.json`（summary、where、size、status `planned`）。
4. 同時想好建築要用的表面材質與地形層（surface item，之後建造階段下載；這裡先列出來）。

## 2. 空間概念圖（每個小空間都做）

```bash
python w2w.py comfy start
python w2w.py concept <world> <space> --prompt "<這個小空間：誰在用、什麼狀態、什麼光線、主要家具>" --n 3
python w2w.py concept <world> <space> --prompt "..." --ref concepts/<space>/concept_1.png   # 在喜歡的那張上加細節
python w2w.py sheet <world> <space>
```

- Read 每張概念圖。寫進 `plan/spaces/<space>.json` 的 `concepts[]`：**取用了什麼**（物件怎麼成群、高低層次、留白、光的方向、顏色），而不是描述整張圖。下一個 agent 只能看到你寫下的東西。
- 從建造階段交接過來時，交接訊息會附引擎截圖：用 `--ref shots/rNN/<cam>.png` 讓概念圖建立在現有格局上。

## 3. 物件清單

依 ART_RULES 第 1、2 節，從概念圖與描述拆出 item，寫成一個 JSON 清單，`python w2w.py add <world> <list.json>`：

- 能單獨拿起的就是獨立 item；一張桌子 8–15 件，一個房間 40–90 種。
- 主要家族都要有 2–3 個變體（`variant_of` + `edit`）；打開的櫃子、抽屜、箱子要另外列出內容物。
- 平面影像（相片、海報、畫）用 `slab`；草、蕨、花用 `card`；痕跡用 `decal`（第 5 節的錨點各需要什麼）；樹做成厚實整體造型的 `prop`。
- 每個 item 都要 `size`（真實公尺）、`tier`、`spaces`。
- 不要做：透明玻璃件（玻璃部分會變白色實心：生成時去掉，引擎補）、文字（亂碼：用 slab）、蔓延的藤蔓根系（用 card 或 decal）、大片平板（建築做）。
- 完成後把 item id 填進對應的 `plan/spaces/<space>.json` 的 `items` 與 `zones`。

## 4. 2D 迭代（每個 item）

```bash
python w2w.py gen <world> <id>                                          # 第一張
python w2w.py gen <world> <id> --review "椅腳被裁掉" --prompt "<針對缺點改寫的完整 prompt>"
python w2w.py gen <world> <id> --review "合格"                           # 不改 prompt，換種子多一張
python w2w.py pick <world> <id> <n> --why "四隻椅腳完整，木紋最自然"
```

- 一次一張、Read 每一張、寫評語再決定下一步。物件多且 prompt 已經穩定時，可以先 `python w2w.py batch <world>` 每個一張，只對不合格的迭代。
- 父物件先選定，變體才能生成（`batch` 會自動排在後面）；變體不合格時改 `--prompt`（會寫回 `edit`）。
- 結束條件：≥ 2 張且至少 1 張合格；6 張仍不合格就選最接近的並在 item 的 `note` 寫明。

| 審核 | 不合格的樣子 |
|---|---|
| 單一物件 | 多了別的東西、多一個同款、有人、有地面或桌面 |
| 完整 | 被畫面邊緣裁掉、沒有留白 |
| 符合描述 | 種類、顏色、部件數量、狀態不符 |
| 風格一致 | 和同空間的其他物件不像同一個世界 |
| 適合 3D | 看不出厚度、極端透視、細件糊在一起、透明部件 |
| 變體 | 和父物件不像同一件東西（造型、材質、顏色跑掉） |
| 貼花 | 背景不是純白、痕跡太密太均勻、碰到畫框邊 |

全部選完：`python w2w.py comfy stop`。

## 5. 去背

```bash
python w2w.py matte <world>
```

Read 每個 `2d/review.png`：物件完整、沒有白邊殘影、細件沒被吃掉、白色物件沒被挖空。不合格就 `pick` 另一張後重跑 `matte <world> <id>`。

## 6. TRELLIS（背景執行 + 每 30 分鐘回報）

1. 用 Bash 的 `run_in_background` 執行 `python w2w.py trellis <world>`（模型只載入一次，分三輪：全部 1024/18 步 → 失敗的 1024/12 步 → 仍失敗的 512；失敗記在 `3d/trellis_fail.json` 的 `level`；CUDA 錯誤（如 device not ready）會弄壞記憶體配置器，所以那時會重開行程並從記錄的 level 繼續；`trellis.pipeline_type`、`trellis.steps` 可逐件固定）。
2. **立刻建立 30 分鐘的進度回報**：用排程工具（CronCreate，cron `*/30 * * * *`，或 `/loop 30m`），內容是「執行 `python w2w.py progress <world>`，用一行告訴使用者完成數、目前物件、預估剩餘時間」。
3. 等待時可以做不需要 GPU 的事：`python w2w.py decals <world>`（只處理已選圖的貼花）、整理 `plan/spaces`、寫 surface item 的規格。
4. 背景工作結束（收到通知，或 progress 顯示 `finished` / `NOT RUNNING`）後：**刪除那個排程**（CronDelete），再回報最後一次結果。
5. Read 每個 `3d/preview.png`（上排著色、下排法線）：形狀對不對、有沒有破洞或多餘的塊。壞的：換 `trellis.seed` 或換圖後重跑 `python w2w.py trellis <world> <id>`；每件最多重做 2 次，之後在 `note` 註明。

時間參考（RTX 3060）：TRELLIS 載入約 5 分鐘、1024 每件約 2–6 分鐘；FLUX 冷啟動 2–4 分鐘、之後約 10–20 秒一張。

## 7. 結束與交接

1. `python w2w.py status <world>`：沒有卡在 `planned` / `drafting` / `picked` / `matted` 的（有的話寫原因）。
2. `python w2w.py decals <world>`。
3. 確認 ComfyUI 與 TRELLIS 都已停止（`python w2w.py comfy status`、`progress`）。
4. `python w2w.py handoff <world>`，然後**在對話裡**寫一段交接 prompt（不要另存檔案），讓使用者貼給下一個 agent：

```
/world-build <world>
本輪鍛造完成：<n> 個新 item 已到 ready（<類別:數量>）；<m> 個放棄（原因）。
空間與概念：<space> 取用概念圖 <n>（重點一句話）；…
要特別注意：<yaw 可能要修的模型、某些變體的用途、哪些 item 是給哪個佔位物的>
建議建造順序：<先做什麼>
```

只回報實際看過、驗證過的結果；沒看過的要明說。

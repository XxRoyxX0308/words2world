---
name: world
description: words2world 的完整流程：文字或圖片描述 → Unreal 場景。依序跑四個階段：1 規劃與初步建築（Blender / Unreal）→ 2 概念圖與 2D（ComfyUI）→ 3 去背與 3D（TRELLIS）→ 4 營造場景（Blender / Unreal），截圖審查覺得缺東西就回到 2。每個階段開始前檢查對話上下文用量，超過上限就寫交接 prompt 停下。使用時機：開始一個新世界、接手交接 prompt、或使用者要改某個世界的場景。
---

# world：從描述到場景

指令一律在 repo 根目錄用 Windows Python：`python w2w.py <command> <world> ...`（`python w2w.py` 列出全部）。
標準在 [docs/ART_RULES.md](../../../docs/ART_RULES.md)，格式在 [docs/FORMATS.md](../../../docs/FORMATS.md)：開始前全部讀過。

```
1 規劃與初步建築 → 2 概念圖與 2D → 3 去背與 3D → 4 營造場景 ─┐
                    ↑                                            │ 截圖審查還缺東西
                    └────────────────────────────────────────────┘
```

## 顯卡由指令切換

12 GB 顯卡一次只能給一種工作，但不用人去開關程式：

- `matte` / `trellis` 會停掉 ComfyUI，等 w2w 自己開的無介面 Blender / Unreal 跑完再開始。
- `bake` / `arch` / `terrain` / `engine` 會拒絕在 TRELLIS 或去背還在跑時執行，並停掉全速模式的 ComfyUI。
- `comfy start` 偵測到 Blender / Unreal 在跑就自動加 `--lowvram`。
- **使用者自己開的 Blender / Unreal 視窗，指令只會拒絕、不會關掉**（可能有未存檔的東西）。遇到時請使用者存檔關閉，不要自己 kill。

其他硬性限制：不在 conda env `trellis2` 安裝或升級套件；不印出 `~/.cache/huggingface/token`；FLUX 只用 klein 4B；pkill / pgrep 樣式第一個字母包成 `[x]`。

## 階段檢查點（每個階段開始前，含迴圈中的每一次）

1. `python w2w.py context`（知道自己的 session id 時加 `--session <id>`：就是 scratchpad 路徑中那段 UUID）。
2. 顯示 `CONTEXT_OK` → 開始這個階段。階段進行中不再檢查。
3. 顯示 `CONTEXT_FULL`（已用超過上限，預設 70%，`w2w.config.json` 的 `context_window` / `context_limit` 可改）→ **不要開始這個階段**：
   - 確認沒有東西在背景跑（TRELLIS 在第 3 階段內就會結束；ComfyUI 停掉）。
   - `python w2w.py handoff <world>`，在對話裡寫下面的交接 prompt（不要存成檔案）。
   - 告訴使用者兩個選擇：貼到新對話；或在這個對話執行 `/compact` 後說「繼續」。agent 不能自己執行 `/compact`。

```
/world <world>
從第 <N> 階段（<名稱>）開始，這是第 <k> 輪。
已完成：<上一個階段的結果：數量、通過審查的、放棄的與原因>
這個階段要做：<handoff 的 todo；佔位物要的東西與參考的概念圖 / 截圖>
要注意：<還沒驗證的、使用者說過的限制、哪些模型品質較低>
```

下一個 agent 只能看到交接 prompt 與磁碟上的檔案：細節寫進 `plan/spaces/*.json` 的 `notes_for_next` 與 item 的 `note`，prompt 只放重點。

## 0. 接手時

`python w2w.py status <world>` 與 `handoff <world>`：`next_phase` 就是要從哪個階段開始（交接 prompt 寫的優先）。讀 `plan/spaces/*.json`、最新的 `shots/r*/`。

## 1. 規劃與初步建築（Blender、Unreal）

新世界：

1. `python w2w.py new <world> --brief <描述檔>`；使用者給的圖放 `refs/`。讀完整描述，填 `plan/world.json` 的 `style`（look / palette / materials / avoid），之後所有 prompt 共用。
2. 把世界切成**小空間**（一個房間、一面外牆、門廊、角落、小徑、林間空地），每個寫 `plan/spaces/<space>.json`（summary、where、size、status `planned`）。
3. 建築：依 ART_RULES 第 4 節寫 `build/architecture.json`（基座、牆身層次、頂飾、門窗框與窗台與楣、轉角、挑簷 / 封簷 / 屋脊、煙囪、雨槽、門廊、室內踢腳與樑）→ `python w2w.py arch <world>`，Read 每張 `build/out/arch/preview_<building>.png` 逐項對照，修到沒有一面是平板盒子。預覽是分槽的假色，看形狀不看顏色。
4. 地形：`build/terrain.json`（起伏、遠景山脊、基地 `flatten`、小徑、溪溝；四層 surface）→ `python w2w.py terrain <world>`。植被可以先用佔位方塊確認分布。
5. 建築與地形用到的每個 surface 都要是 library 的 `kind: surface` item。`python w2w.py surfaces <world>` 下載前先告訴使用者來源（Poly Haven / ambientCG）、解析度與大約大小（2k 一組約 20–60 MB）。
6. `build/scene.json` 放鏡頭（每個空間一張全景、一張中景）與燈，`python w2w.py engine <world> --shots`。**第一次對某個 Unreal 專案匯入前**告訴使用者：會寫入 `/Game/W2W/`，會開一個編輯器視窗、跑完自己關，請不要關。不改專案設定或外掛；日誌出現 `WARNING project setting` 轉告使用者。
7. Read 每張截圖：這些空殼截圖就是第 2 階段概念圖的 `--ref`，讓概念建立在真正的格局與光線上。

## 2. 概念圖與 2D（ComfyUI）

1. `python w2w.py comfy start`。
2. 每個小空間（迴圈時只做缺東西的空間）：

```bash
python w2w.py concept <world> <space> --ref shots/rNN/<cam>.png --prompt "<誰在用、什麼狀態、什麼光線、主要家具>" --n 3
python w2w.py concept <world> <space> --prompt "..." --ref concepts/<space>/concept_1.png   # 在喜歡的那張上加細節
python w2w.py sheet <world> <space>
```

   Read 每張，寫進 `plan/spaces/<space>.json` 的 `concepts[]`：**取用了什麼**（怎麼成群、高低層次、留白、光的方向、顏色），不是描述整張圖。
3. 物件清單（ART_RULES 第 1、2 節）寫成 JSON，`python w2w.py add <world> <list.json>`：
   - 能單獨拿起的就是獨立 item；一張桌子 8–15 件，一個房間 40–90 種。
   - 主要家族要有 2–3 個變體（`variant_of` + `edit`）；打開的櫃子、抽屜、箱子另列內容物。
   - 相片、海報、畫用 `slab`；草、蕨、花用 `card`；痕跡用 `decal`；樹做成厚實整體的 `prop`。
   - 每個 item 要有 `size`（公尺）、`tier`、`spaces`；完成後填進 `plan/spaces/<space>.json` 的 `items` 與 `zones`。
   - 不做：透明玻璃件（引擎補）、文字（用 slab）、蔓延的藤蔓根系（card 或 decal）、大片平板（建築做）。
4. 2D 迭代：

```bash
python w2w.py gen <world> <id>                                          # 第一張
python w2w.py gen <world> <id> --review "椅腳被裁掉" --prompt "<針對缺點改寫的完整 prompt>"
python w2w.py gen <world> <id> --review "合格"                           # 不改 prompt，換種子多一張
python w2w.py pick <world> <id> <n> --why "四隻椅腳完整，木紋最自然"
```

   一次一張、Read 每一張、寫評語再決定下一步；prompt 穩定後可以先 `batch <world>` 每件一張，只迭代不合格的。父物件先選定，變體才能生成。≥ 2 張且至少 1 張合格就選；6 張仍不合格就選最接近的並在 `note` 寫明。

| 審核 | 不合格的樣子 |
|---|---|
| 單一物件 | 多了別的東西、多一個同款、有人、有地面或桌面 |
| 完整 | 被畫面邊緣裁掉、沒有留白 |
| 符合描述 | 種類、顏色、部件數量、狀態不符 |
| 風格一致 | 和同空間的其他物件不像同一個世界 |
| 適合 3D | 看不出厚度、極端透視、細件糊在一起、透明部件 |
| 變體 | 和父物件不像同一件東西 |
| 貼花 | 背景不是純白、痕跡太密太均勻、碰到畫框邊 |

5. 全部選完：`python w2w.py comfy stop`。

## 3. 去背與 3D（整張顯卡）

1. `python w2w.py matte <world>`，Read 每個 `2d/review.png`：物件完整、沒有白邊殘影、細件沒被吃掉、白色物件沒被挖空。不合格就 `pick` 另一張，重跑 `matte <world> <id>`。
2. Bash `run_in_background` 執行 `python w2w.py trellis <world>`。模型只載入一次，分三輪：全部 1024/18 步 → 失敗的 1024/12 步 → 仍失敗的 512；失敗記在 `3d/trellis_fail.json` 的 `level`。CUDA 錯誤（如 device not ready）會弄壞記憶體配置器，此時行程重開並從記錄的 level 繼續。`trellis.pipeline_type`、`trellis.steps` 可逐件固定，但不要為了單一次失敗去固定。
3. **立刻**建立 30 分鐘的進度回報（CronCreate `*/30 * * * *`）：執行 `python w2w.py progress <world>`，用一行告訴使用者完成數、目前物件、預估剩餘時間。
4. 等待時做不需要 GPU 的事：`python w2w.py decals <world>`、整理 `plan/spaces`、寫下一階段的 scene.json 草稿。
5. 結束（收到通知，或 progress 顯示 `finished` / `NOT RUNNING`）→ **刪除排程**（CronDelete）。
6. Read 每個 `3d/preview.png`（上排著色、下排法線）：形狀對不對、有沒有破洞或多餘的塊。壞的換 `trellis.seed` 或換圖後 `trellis <world> <id>`；每件最多重做 2 次，之後在 `note` 註明。

時間參考（RTX 3060）：TRELLIS 載入約 5 分鐘，1024 每件 2–6 分鐘；FLUX 冷啟動 2–4 分鐘、之後 10–20 秒一張。

## 4. 營造場景（Blender、Unreal、ComfyUI --lowvram）

1. 新物件：

```bash
python w2w.py bake <world>          # prop / slab / card → game/（FBX、LOD、貼圖、preview.png）
python w2w.py decals <world>
python w2w.py surfaces <world>      # 有新的 surface 才需要；下載前照第 1 階段告知使用者
```

   讀 `build/out/bake_report.md`：`review` / `check` / `fail` 的 Read `game/preview.png` 處理（AO 全黑 → `bake.remesh`；法線錯 → `bake.cage` / `ray`；LOD 變形 → `decimate: planar`；密度低 → `bake.uv: smart`），每件最多重做 2 次。正面不是朝 −Y 就設 `yaw_fix` 再 `bake <world> <id>`。GLB 本身壞掉不要硬修：寫 `note`，下一輪第 3 階段重做。可用 Blender MCP 即時調參：`runpy.run_path(r"<repo>/blender/bake_items.py")["tune"](r"<world_dir>", "<id>", cage=0.012)`。
2. 依新物件修建築與地形（第 1 階段的檢查清單），讓出空間、補建築腳邊的過渡。
3. 擺放（`build/scene.json` → `scene`）：一個空間一個空間做，重讀 `concepts[]` 筆記並 Read 那張概念圖，照它的聚落與層次擺，不要沿牆排一圈。小物用 `on` 疊在父物件上；書、罐、木柴用 `arrays`；地上零碎用 `scatter`。貼花先用 `decal_rules` 鋪滿建築錨點，再手放重點（數量見 ART_RULES 第 5 節）。燈：主光 + 開陰影的重點光（ART_RULES 第 8 節）。鏡頭：每空間全景、中景，主角物件近景，戶外遠景。
4. `python w2w.py engine <world> --shots`（背景執行）。unreal-mcp 連得上而且使用者開著編輯器時，可以用 `--live` 印出的指令在那個編輯器裡跑。
5. Read `build/out/unreal_import.log` 與 `shots/r<NN>/` 的**每一張**截圖，修 json 後重跑（每輪最多 3 次）：
   - 格局、尺寸、動線與描述一致；建築沒有一面像平板盒子，交界處有東西打斷；
   - 沒有看得出的貼圖重複，沒有 3 × 3 m 以上的空平面；
   - 每個空間的物件量達到 ART_RULES 第 1 節，有聚落與層次；貼花在該在的地方且數量足夠；
   - 沒有懸空或穿插；遠 / 中 / 近都有細節；光線方向與色溫符合描述；
   - `budget.json` 沒有未處理的 `BUDGET` 警告。

## 迴圈與結束

- **還缺東西**：`comfy start`（會自動 `--lowvram`），用截圖要概念：`concept <world> <space> --ref shots/rNN/<cam>.png --prompt "this exact room and camera; add <想要的層次>"`。Read 結果，把缺的東西 `add` 成 `planned` item，在 scene.json 放它的實例（引擎裡是洋紅色 `TEMP_<item>` 方塊，模型做好後自動替換），`notes_for_next` 寫清楚哪個佔位要什麼、為什麼。然後回到第 2 階段，只處理新的 item（先做檢查點）。
- **上限**：連續 3 輪迴圈後仍不滿意，停下來給使用者看截圖與缺什麼，讓他決定要不要再跑。
- **完成**（審查清單全部通過、沒有 `planned` item、沒有佔位物）：`comfy stop`，更新每個 `plan/spaces/<space>.json` 的 `status`，給使用者總結：空間表（尺寸、與描述的差異）、物件數（種類、實例）、建築部件與材質來源、貼花數、植被數、`budget.json` 總數、各階段耗時（`log.jsonl`）、放棄或妥協的項目、需要使用者補的（互動、門的動畫、音效）。

只回報實際看過、驗證過的結果；沒看過的要明說。

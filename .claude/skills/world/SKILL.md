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
6. `build/scene.json` 放鏡頭與燈。鏡頭要能看到每個空間的全部：每個房間至少從兩個相對的角落各拍一張（大房間或 L 形、多段的空間再加，直到每面牆、每個角落都至少在一張裡），加上主角物件的中景；戶外每個方向至少一張。只有一個角度時，鏡頭背後的半個房間等於沒審查過。`python w2w.py engine <world> --shots`。**第一次對某個 Unreal 專案匯入前**告訴使用者：會寫入 `/Game/W2W/`，會開一個編輯器視窗、跑完自己關，請不要關。不改專案設定或外掛；日誌出現 `WARNING project setting` 轉告使用者。
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

   烘焙前先給每個新 prop 設 `bake.faces`（LOD0 三角面上限）：小物約 2,500、家具與設備約 6,000、細桿和網格多的約 12,000，主角物件和車輛再高一些。沒有設就是 0，會保留 TRELLIS 的原始面數。LOD1、LOD2 會自動依距離產生。設太低會破洞或撕裂貼圖，烘焙後看 preview；有破洞的就調高，並在 `note` 寫明。減面後的模型會重新展 UV，顏色從原模型轉烤過來（`bake_report` 的 `uv: transfer`）。沿用 TRELLIS 的 UV 直接減面時，三角形會跨過 UV 島之間的空隙，貼圖上就出現黑色碎片。所以減面之後的 preview 要看有沒有黑色碎片，也要看顏色有沒有比 2D 圖暗。TRELLIS 常把平板做成中空的盒子，內壁離外皮只有幾 mm，AO 幾乎全黑，在引擎裡會從外皮透出成一片片暗影。烘焙會刪掉這種內壁（`hollow`）；`AO black` 偏高的物件，要看是不是內壁沒刪乾淨。刪內壁會改變網格的拓撲，所以要放在清理的最後；先刪會讓「內部面」那一步把外皮也當成內部刪掉，模型變成碎片。烘焙完要看整批 preview 有沒有碎掉的。百葉、格柵這類很密的細件，減面會撕開，就保留原始面數（`bake.faces: 0`）。

   跑 `engine` 前看 `scene` 的輸出：出現 `not processed yet`，就是那些貼花或物件的 item.json 比成品新，先重跑 `decals` 或 `bake`。貼花數掉到 0，一定是這個原因。
   讀 `build/out/bake_report.md`：`review` / `check` / `fail` 的 Read `game/preview.png` 處理（AO 全黑 → `bake.remesh`；法線錯 → `bake.cage` / `ray`；LOD 變形 → `decimate: planar`；密度低 → `bake.uv: smart`），每件最多重做 2 次。
   門、櫃門這類有把手的板子，2D 要畫正視圖：從斜側看，TRELLIS 會把把手鏡像到另一側。雙開門做成一扇，場景裡擺兩扇，另一扇用 `scale: [-1, 1, 1]` 鏡像。
   **朝向要逐件確認，不只看 `check`。** 烘焙把每個軸各自縮放到 `size`，TRELLIS 模型如果側躺（正面朝 ±X），尺寸照樣對，只是整件被壓扁，桌子會變成抽屜朝側邊、椅子背對桌子。所有有正反面的物件（桌、椅、櫃、台、車、設備）都要看 preview 下排兩格：左下是從 −X 看，右下是從 −Y 看，正面應該在右下。
   - 正面在左下：設 `yaw_fix: 90`。
   - 正面在 −X 的另一側（上排背面那格才看得到）：設 `yaw_fix: 270`。
   - 正面朝 +Y：設 `yaw_fix: 180`。

   改完 `bake <world> <id>`。寬的物件長邊落在深度方向時，`bake_report` 會標 `check`。GLB 本身壞掉不要硬修：寫 `note`，下一輪第 3 階段重做。可用 Blender MCP 即時調參：`runpy.run_path(r"<repo>/blender/bake_items.py")["tune"](r"<world_dir>", "<id>", cage=0.012)`。
2. 依新物件修建築與地形（第 1 階段的檢查清單），讓出空間、補建築腳邊的過渡。
3. 擺放（`build/scene.json` → `scene`）：一個空間一個空間做，**每個空間都要擺，包括沒有概念圖的**。重讀 `concepts[]` 筆記並 Read 那張概念圖，理解它這樣擺的理由，再自己決定整個房間（四面牆、角落、中央、鏡頭背後）怎麼擺；概念圖不合理的地方照用途自己改，不夠就用新的 prompt 或不同方向再生成（ART_RULES 第 3 節）。不要沿牆排一圈；大空間分成多個聚落，物件量跟著面積增加。用平面圖（俯視、畫出每個實例的外框）檢查整個房間有沒有空出一大片，不要只看鏡頭拍到的那一面。座位的朝向從「人坐下來看哪裡」來算：椅子的 yaw 用 `face(桌心 − 椅子位置)`，不是椅子相對桌子的方位角（差了 180°）。桌子的正面是使用者那一側，抽屜朝向使用者。擺完在平面圖（實例外框有朝向線）上確認。同類房間不要整排只用同兩三種物件：每種用途準備多個種類與變體，按房間輪流挑。小物用 `on` 疊在父物件上；書、罐、木柴用 `arrays`；地上零碎用 `scatter`。貼花先用 `decal_rules` 鋪滿建築錨點，再手放重點（數量見 ART_RULES 第 5 節）。燈：主光 + 開陰影的重點光（ART_RULES 第 8 節）。鏡頭：同第 1 階段第 6 步，每個空間多個方向（至少兩個相對角落，大空間更多），加主角物件近景與戶外遠景；新擺了東西的空間就補鏡頭，審查時把同一空間的幾張放在一起看。鏡頭前要先空出來，再擺家具：
   - 選離門較遠的那一對角落，免得開著的門板貼在鏡頭旁；
   - 角落沿兩面牆約 2 m 內不放高櫃；
   - 手擺鏡頭的鏡頭前方約 2 m 留空；
   - 鏡頭前方 ±35°、2 m 內不能有擋住的東西（廁所隔間的側板、高櫃、開著的門板）；擋住就沿對角線往房間裡移，或換一個角落；
   - 截圖還是太暗，就加強那台鏡頭的手電筒。

   屋頂與平台也是空間，要和房間一樣擺。設備集中成幾群，管線把各群連起來，再加步道、排水口、安全錨點和水漬。屋面上有天窗、植栽槽、女兒牆和建築本身的設備箱，所以空地要從建好的建築往下打射線求得（像 `roof_height.py` 那樣），不要手寫排除範圍。

   落地玻璃也是「門洞」（窗台高度 0）。寬的開口不要當門，前方的地板不必保留，不然整個房間會擺不下東西。
4. `python w2w.py engine <world> --shots`（背景執行）。unreal-mcp 連得上而且使用者開著編輯器時，可以用 `--live` 印出的指令在那個編輯器裡跑。
5. Read `build/out/unreal_import.log` 與 `shots/r<NN>/` 的**每一張**截圖，修 json 後重跑（每輪最多 3 次）：
   - 格局、尺寸、動線與描述一致；建築沒有一面像平板盒子，交界處有東西打斷；
   - 沒有看得出的貼圖重複，沒有 3 × 3 m 以上的空平面；
   - 每個空間的物件量達到 ART_RULES 第 1 節，有聚落與層次；貼花在該在的地方且數量足夠；
   - 沒有懸空或穿插；遠 / 中 / 近都有細節；光線方向與色溫符合描述；
   - `budget.json` 沒有未處理的 `BUDGET` 警告。三角面超標時降低模型的面數（item 的 `bake.faces` 調低 LOD0，重複很多次的小物先降），再 `bake <world> <id>`；不要用拿掉物件的方式過預算，物件數量是場景看起來有人用過的來源。貼花超標先檢查是否某個空間太大（拆成幾個空間），再減少重複的同一張。

## 迴圈與結束

- **還缺東西**：`comfy start`（會自動 `--lowvram`），用截圖要概念：`concept <world> <space> --ref shots/rNN/<cam>.png --prompt "this exact room and camera; add <想要的層次>"`。Read 結果，把缺的東西 `add` 成 `planned` item，在 scene.json 放它的實例（引擎裡是洋紅色 `TEMP_<item>` 方塊，模型做好後自動替換），`notes_for_next` 寫清楚哪個佔位要什麼、為什麼。然後回到第 2 階段，只處理新的 item（先做檢查點）。
- **上限**：連續 3 輪迴圈後仍不滿意，停下來給使用者看截圖與缺什麼，讓他決定要不要再跑。
- **完成**（審查清單全部通過、沒有 `planned` item、沒有佔位物）：`comfy stop`，更新每個 `plan/spaces/<space>.json` 的 `status`，給使用者總結：空間表（尺寸、與描述的差異）、物件數（種類、實例）、建築部件與材質來源、貼花數、植被數、`budget.json` 總數、各階段耗時（`log.jsonl`）、放棄或妥協的項目、需要使用者補的（互動、門的動畫、音效）。

只回報實際看過、驗證過的結果；沒看過的要明說。

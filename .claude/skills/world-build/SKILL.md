---
name: world-build
description: words2world 的「營造」階段（引擎階段）：把鍛造好的物件烘焙成遊戲資產，在 Unreal 裡建出建築（不是方盒）、地形與植被，擺放物件、貼花、燈光，截圖審查；覺得空的地方用 ComfyUI 概念圖（可附引擎截圖）想出還缺什麼，留下佔位物並交接回鍛造階段。使用時機：鍛造階段交接「物件已 ready」，或使用者要在引擎裡組／改場景。
---

# world-build：組場景（引擎階段）

這個階段會開 Blender（無介面）與 Unreal。可以用 ComfyUI（`--lowvram`）畫概念圖，**不能跑 TRELLIS 或去背**：需要新模型就列出來交給 `world-forge`。

指令：`python w2w.py <command> <world> ...`。標準：[docs/ART_RULES.md](../../../docs/ART_RULES.md)（全部讀過，第 3–7 節是這個階段的重點）；格式：[docs/FORMATS.md](../../../docs/FORMATS.md)。

## 0. 開始前

1. `python w2w.py status <world>`；讀交接訊息、每個 `plan/spaces/*.json`、最新一輪 `shots/r*/` 與 `build/out/budget.json`。
2. 確認 TRELLIS 沒在跑（`python w2w.py progress <world>`；build 指令也會擋）。
3. **第一次對某個 Unreal 專案匯入前**，告訴使用者會寫入 `/Game/W2W/`（Master、`<world>/Library|Arch|Terrain|Maps`），而且會開一個編輯器視窗、跑完自己關，請他不要關。不要改專案設定或外掛；匯入日誌出現 `WARNING project setting` 時轉告使用者，由他決定。
4. 如果 unreal-mcp 連得上，也可以在開著的編輯器裡跑匯入器（`python w2w.py engine <world> --live` 會印出指令）；連不上就用一般模式（匯入器自己開關編輯器，此時編輯器必須是關的）。

## 1. 處理新物件

```bash
python w2w.py bake <world>          # prop / slab / card → game/（FBX、LOD、貼圖、preview.png）
python w2w.py decals <world>
python w2w.py surfaces <world>      # 第一次下載前告訴使用者：來源、解析度、大約大小（2k 一組約 20–60 MB）
```

- 讀 `build/out/bake_report.md`：`ok` 不用看；`review` / `check` / `fail` Read `game/preview.png` 處理（AO 全黑 → `bake.remesh`；法線錯 → `bake.cage` / `ray`；LOD 變形 → `decimate: planar`；密度低 → `bake.uv: smart`）。每件最多重做 2 次。
- 所有新 preview 掃一眼正面方向：正面不是朝 −Y 就設 `yaw_fix` 再 `bake <world> <id>`。
- GLB 本身壞掉（破洞、形狀錯）不要硬修：在 item 寫 `note`，交接給鍛造重做。
- 需要即時調參時可以用 Blender MCP：`runpy.run_path(r"<repo>/blender/bake_items.py")["tune"](r"<world_dir>", "<id>", cage=0.012)`，在 `W2W_Tune` 場景看結果。

## 2. 建築（`build/architecture.json` → `python w2w.py arch <world>`）

- 先依 ART_RULES 第 4 節的檢查清單設計：基座、牆身層次、頂飾、門窗框 / 窗台 / 楣、轉角、挑簷 / 封簷 / 屋脊、煙囪、雨槽、門廊、室內踢腳與樑。
- 沒有把握外觀怎麼做：`python w2w.py comfy start --lowvram`，對建築外觀生成 2–3 張概念圖，照著拆部件。
- 每次 `arch` 後 Read `build/out/arch/preview_<building>.png`，逐項對照清單；修到沒有一面是平板盒子。
- 建築用到的每個 `surface` 都要在 library（`kind: surface`），`layer2` / `macro` / `grime` 依 ART_RULES 第 6 節設定。

## 3. 地形與植被（`build/terrain.json` → `python w2w.py terrain <world>`）

- 地形：基礎起伏 + 遠景山脊 + 建築基地 `flatten` + 小徑 `paths` + 必要時溪溝 `carve`；`layers` 指定草 / 泥土 / 岩石 / 林地四個 surface。
- 植被分層（ART_RULES 第 7 節）：樹冠、林下、地被（card）、散物；用 `avoid` 讓出建築與小徑，但建築腳邊要有過渡。
- 植物 item 還沒烘焙時也可以先跑：它們會以佔位方塊出現（每種最多 400 個），讓你先確認分布。

## 4. 擺放（`build/scene.json` → `python w2w.py scene <world>`）

- 一個空間一個空間做：重讀 `plan/spaces/<space>.json` 的概念圖筆記，再 Read 那張概念圖。照它的**聚落方式與層次**擺，不是沿牆排一圈。
- 小物用 `on` 疊在父物件上（桌面、層架的 `level`）；書、罐子、木柴用 `arrays`；地上的零碎用 `scatter`。
- 想放但 library 沒有的東西：先 `python w2w.py add` 成 `planned` item（寫清楚 prompt、size、它要放在哪），再在 scene.json 放它的實例 → 引擎裡會是洋紅色 `TEMP_<item>` 方塊，鍛造完成後下一輪自動換成模型。
- 貼花：`decal_rules` 鋪滿建築錨點（牆基、轉角、窗下、門框、屋簷下、地板邊），再手放重點（壁爐上方煤煙、門口踩踏、桌下磨損）。數量見 ART_RULES 第 5 節。
- 燈：主光（太陽 / 主燈）、重點光（窗邊、桌燈、火光，開陰影），數值見 ART_RULES 第 8 節；燈具本身用 `fixture`。
- 鏡頭：每個空間至少一張全景、一張中景；主角物件加一張近景；戶外加一張遠景（看天際線與植被層次）。

## 5. 匯入與審查（每輪）

```bash
python w2w.py engine <world> --shots       # 背景執行；會開編輯器、跑完自己關
```

Read `build/out/unreal_import.log` 與 `shots/r<NN>/` 的**每一張**截圖，逐項核對，修 json 後重跑（每輪最多 3 次）：

- 格局、尺寸、動線與描述一致；
- 建築沒有任何一面像平板盒子；交界處（牆與地、牆與牆、牆與屋頂）有東西打斷；
- 沒有看得出的貼圖重複；沒有 3 × 3 m 以上的空平面；
- 每個空間的物件量達到 ART_RULES 第 1 節的數字，有聚落與層次；
- 貼花在該在的地方，數量足夠；
- 沒有懸空或穿插（底面在地上、牆掛件貼牆、疊放的東西在表面上）；
- 遠 / 中 / 近三個距離都有細節；光線方向與色溫符合描述；太暗太亮先調燈與曝光範圍；
- `budget.json` 沒有未處理的 `BUDGET` 警告。

## 6. 覺得還少東西時：用截圖要概念

```bash
python w2w.py comfy start --lowvram
python w2w.py concept <world> <space> --ref shots/r03/<cam>.png --prompt "this exact room and camera; add <想要的層次：牆上掛滿的工具、窗台的瓶罐、地上的籃子與木柴>" --n 3
python w2w.py comfy stop
```

Read 結果，把取用的重點寫回 `plan/spaces/<space>.json` 的 `concepts[]`，拆出新的 item（第 4 步的佔位做法），`notes_for_next` 寫清楚「哪個佔位要什麼、為什麼」。

## 7. 結束與交接

1. 關掉 ComfyUI（`python w2w.py comfy stop`）；Unreal 由匯入器自己關，確認沒有殘留的編輯器（下一個鍛造階段需要整張顯卡）。
2. 更新每個 `plan/spaces/<space>.json` 的 `status` 與 `notes_for_next`。
3. `python w2w.py handoff <world>`。
4. 有 `planned` item（新的佔位）→ 在對話裡寫交接 prompt 給鍛造階段：

```
/world-forge <world>
建造第 <N> 輪完成（截圖 shots/r<NN>/）。需要生成 <n> 個新 item（已 add 為 planned）：
- <space>：<id 清單> — 用途 / 擺在哪 / 參考 concepts/<space>/concept_<k>.png
需要重做：<id>（原因：GLB 破洞 / 造型不對）
概念圖可附截圖：shots/r<NN>/<cam>.png
```

5. 沒有新 item → 給使用者總結：空間表（尺寸、與描述的差異）、物件數（item 種類、實例、佔位剩幾個）、建築部件與材質來源、貼花數、植被數、`budget.json` 總數、各階段耗時（`log.jsonl`）、放棄或妥協的項目、需要使用者補的（互動、門的動畫、音效）。

只回報實際看過、驗證過的結果；沒看過的要明說。

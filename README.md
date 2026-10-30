# words2world

把一段文字（或幾張參考圖）變成 Unreal Engine 5 裡可以走進去的場景。由 AI agent（Claude Code）用一個 skill `/world` 執行，分四個階段，覺得缺東西就回到第 2 階段：

```
1 規劃與初步建築 ──► 2 概念圖與 2D ──► 3 去背與 3D ──► 4 營造場景 ─┐
 (Blender、Unreal)     (ComfyUI)         (RMBG、TRELLIS)  (Blender、Unreal) │
                         ▲                                                  │
                         └──────────── 截圖審查還缺東西 ◄───────────────────┘
```

| 階段 | 做什麼 | 用到 |
|---|---|---|
| 1 規劃與初步建築 | 切小空間 → 參數化建築 → 地形 → 材質 → 第一次匯入與截圖 | Blender、Unreal |
| 2 概念圖與 2D | 以空殼截圖為參考畫概念圖 → 拆物件（含變體）→ 2D 迭代 | ComfyUI |
| 3 去背與 3D | 去背 → TRELLIS 1024（失敗退到 1024/12 步、再退到 512） | RMBG、TRELLIS（整張顯卡） |
| 4 營造場景 | 烘焙資產 → 擺放、貼花、燈光 → 截圖審查 → 缺的東西留佔位物，回到 2 | Blender、Unreal、ComfyUI（`--lowvram`） |

**顯卡**：TRELLIS.2 用 1024 模型時需要整張 12 GB 顯卡。各指令會自己切換：TRELLIS / 去背前停掉 ComfyUI、等 w2w 開的無介面 Blender / Unreal 結束；Blender / Unreal 前確認 TRELLIS 已結束；ComfyUI 在 Blender / Unreal 執行時自動用 `--lowvram`。使用者自己開的 Blender / Unreal 視窗不會被關掉，指令會請你先存檔關閉。

**上下文**：每個階段開始前 agent 會跑 `python w2w.py context`（讀 Claude Code 的對話紀錄算出已用的 token）。超過上限（預設 70%）就不開始下一個階段，改在對話裡寫一段交接 prompt：貼到新對話，或在原對話 `/compact` 後說「繼續」。

## 這個管線特別處理的事

- **物件拆到能單獨拿起來為止**：一張書桌是桌子、螢幕、鍵盤、滑鼠、檯燈、杯子、紙堆…各自生成，再用 `on` / `arrays` / `scatter` 疊放。
- **每個家族都有變體**：以選定的 2D 圖為參考，用 FLUX.2 klein 的參考圖編輯產生「開門的櫃子」「破損的椅子」，再各自轉 3D。
- **概念圖先行**：每個小空間（房間、一面牆、角落）先讓 ComfyUI 畫幾張概念圖，agent 依圖決定擺法；概念圖以引擎截圖為參考圖，建立在真正的格局與光線上。
- **建築不是盒子**：參數化部件（圓木牆、雨淋板、壁柱、腰線、簷口、門窗框與窗台、木瓦屋頂、椽尾、石砌煙囪與基座、碎石、管線、欄杆、樓梯），全部倒角、加權法線、牆基與轉角烘進髒污頂點色。
- **去塑膠、去重複**：貼花依建築錨點批次放（牆基、轉角、窗下、門框、屋簷下）；表面材質有世界座標的 macro 變化、第二層材質混合、頂點色髒污。
- **地形與植被由 agent 做**：高度場（fbm、山脊、溪溝、基地整平、小徑）、四層 splat、Poisson 分布的植被分層（樹冠、林下、地被、散物），引擎端用 HISM。
- **佔位物**：還沒生成的物件在引擎裡是洋紅色 `TEMP_<item>` 方塊，生成後下一輪自動替換。
- **物件依種類分資料夾**：`library/<category>/<family>/<id>/`（例如 `vehicles/sedan/sedan_red_doors_open/`），引擎內容瀏覽器用同一棵樹。

## 需求

| 用途 | 需要 |
|---|---|
| 2D 與 3D 生成 | Windows 11 + WSL2 Ubuntu、NVIDIA 12 GB 以上（RTX 3060 測試）；[TRELLIS.2](https://github.com/microsoft/TRELLIS.2) 在 `~/TRELLIS.2`（conda env `trellis2`）；[ComfyUI](https://github.com/comfyanonymous/ComfyUI) 在 `~/ComfyUI`（conda env `comfyui`），模型 `flux-2-klein-4b-fp8.safetensors`、`qwen_3_4b.safetensors`、`flux2-vae.safetensors`（klein 4B 為 Apache 2.0） |
| 資產處理 | Blender 5.x（無介面執行）；Windows Python 3 + numpy + Pillow + scipy |
| 引擎 | Unreal Engine 5.x（Python Editor Script Plugin、Editor Scripting Utilities；專案需開啟 Generate Mesh Distance Fields 給 Lumen） |
| 選用 | Blender MCP（即時調整烘焙參數）、unreal-mcp（在開著的編輯器裡執行匯入器） |

## 設定

```powershell
copy w2w.config.example.json w2w.config.json    # 填 Blender、UnrealEditor.exe、.uproject 的路徑
pip install numpy pillow scipy
```

## 使用

在 Claude Code 裡（repo 根目錄）：

```
/world 新世界 cozy_cabin：林中一間溫馨的小木屋，傍晚，屋內有壁爐與書桌……
```

上下文用到上限時 agent 會給一段以 `/world cozy_cabin` 開頭、寫明從第幾階段接續的交接 prompt，貼到新的對話即可。

每一步也都是一個指令，可以手動執行（`python w2w.py` 列出全部）：

```powershell
python w2w.py status cozy_cabin              # 每個物件的階段、佔位物、各空間進度
python w2w.py progress cozy_cabin            # TRELLIS 進度
python w2w.py engine cozy_cabin --shots      # 重建關卡並截圖
```

## 資料夾

```
w2w.py                     指令列入口
tools/                     共用（Windows Python）：library、ComfyUI 客戶端、材質、貼花、地形、場景編譯
gpu/                       WSL：ComfyUI 啟停、去背、TRELLIS 批次與重試迴圈
blender/                   無介面 Blender：物件烘焙、建築、地形網格
engine/unreal/             Unreal 匯入器
docs/ART_RULES.md          美術與效能的判斷標準
docs/FORMATS.md            所有 json 的格式
.claude/skills/world/      整個流程的 skill
worlds/<world>/            每個世界的資料（不進版本控制）
```

輸出：

| 位置 | 內容 |
|---|---|
| `worlds/<w>/library/<category>/<family>/<id>/game/` | `SM_<id>.fbx`（+ LOD、UCX 碰撞）、`T_SM_<id>_{BC,N,ORM}.png`、`asset.json`、`preview.png` |
| `worlds/<w>/build/out/` | 建築 FBX 與預覽、地形網格與 splat、`level.json`、`budget.json`、`bake_report.md`、`unreal_import.log` |
| `worlds/<w>/shots/r<NN>/` | 每一輪每台鏡頭的截圖與總覽 `_sheet.png` |
| Unreal `/Game/W2W/` | `Master/`（共用主材質）、`<world>/Library/…`（與磁碟相同的分類樹）、`Arch/`、`Terrain/`、`Maps/<world>` |

重跑是安全的：沒變的資產會跳過；關卡裡帶 `W2W` 標籤的 actor 會重建，手動加的不受影響。

## 授權注意

FLUX.2 klein 4B 為 Apache 2.0；TRELLIS.2 與 RMBG-2.0 依其各自的授權（RMBG-2.0 非商用）。表面材質來自 Poly Haven / ambientCG（CC0）。

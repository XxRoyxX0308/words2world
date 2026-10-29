# 檔案格式

所有座標：公尺，+X 東、+Y 北、+Z 上；yaw 是從上往下看逆時針的角度，物件正面在 yaw 0 時朝 −Y。
每個腳本的開頭 docstring 是該格式最完整的說明；這裡是總覽與範例。

## world 資料夾

```
worlds/<world>/
  brief.md                      使用者的描述（文字）；refs/ 放使用者給的參考圖
  plan/world.json               風格與生成設定（範本：docs/templates/world.json）
  plan/spaces/<space>.json      每個小空間的規劃與概念圖筆記
  concepts/<space>/             concept_<n>.png、prompts.json、sheet.png
  library/<category>/<family>/<id>/
      item.json                 規格
      2d/                       cand_<n>.png、prompts.json、sheet.png、rgba.png、review.png
      3d/                       model.glb、model_high.glb、preview.png、trellis.json
      game/                     引擎用檔案 + asset.json（建造階段產生）
  build/architecture.json       建築部件
  build/terrain.json            地形與植被
  build/scene.json              擺放、貼花、燈、鏡頭
  build/out/                    產生物：arch/、terrain/、shared/、level.json、budget.json、bake_report.md
  shots/r<NN>/                  每一輪的引擎截圖與 _sheet.png
  logs/                         trellis.log、trellis_queue.json、bake_report.json
  log.jsonl                     每一步的耗時
```

## item.json

用 `python w2w.py add <world> <list.json>` 建立（範例：[templates/item_examples.json](templates/item_examples.json)）。

| 欄位 | 說明 |
|---|---|
| `id` | 全世界唯一，小寫英數與底線；也是資料夾名 |
| `category` / `family` | 分類資料夾：`furniture/desk`、`vehicles/sedan`、`foliage/fern`、`decals/water`、`surfaces/wood` |
| `kind` | `prop`（TRELLIS 3D）、`slab`（平面影像：海報、相片、畫、螢幕）、`card`（植物剪影：草、蕨、花）、`decal`、`surface`（無縫材質） |
| `tier` | `hero` / `prop` / `small`：貼圖密度、LOD、TRELLIS 面數 |
| `size` | prop / slab / card：`{"height": 0.76}`、`{"width": 1.3}`、`{"depth": ...}`（給多個 = 各軸分別縮放）；decal：`[寬, 高]` |
| `prompt` | 英文；只描述這一個物件：種類、造型、材質、顏色、關鍵部件、狀態 |
| `variant_of` + `edit` | 變體：以父物件選定的 2D 圖為參考，`edit` 寫要改什麼；未寫的欄位繼承父物件 |
| `refs` | 額外參考圖（相對 world 的路徑），例如概念圖：「把圖中的搖椅單獨畫出來」 |
| `spaces` | 打算用在哪些空間（規劃用） |
| `condition` | `new` / `used` / `worn` / `ruined`（烘焙的髒污與磨損） |
| `surfaces` | 可放東西的高度（公尺），例如書架各層 `[0.05, 0.42, 0.8, 1.18]`；`scene.json` 的 `on` + `level` 會用 |
| `collision` | `box`（預設）/ `convex` / `none`（small 預設 none） |
| `emissive` | 自發光（螢幕、燈罩） |
| `aspect`、`frame` | slab：寬高比與框（`depth`、`border`、`color`） |
| `card` | `{"style": "cross" | "fan" | "flat", "planes": 3}` |
| `alpha`、`roughness`、`opacity`、`normal_strength`、`place` | decal（見 tools/decals.py） |
| `source`、`res`、`tile_m`、`layer2`、`layer2_amount`、`macro`、`grime`、`color` | surface（見 tools/surfaces.py） |
| `trellis` | `{"pipeline_type": "1024_cascade", "faces", "texture", "high_faces", "seed"}`（見 gpu/trellis_batch.py） |
| `bake`、`preset`、`yaw_fix` | 烘焙覆寫（見 blender/bake_items.py） |
| `pick`、`pick_why` | 由 `w2w.py pick` 寫入 |
| `status: "rejected"`、`note` | 放棄並寫明原因與替代做法 |

階段（由檔案推導，不需手寫）：`planned → drafting → picked → matted → ready → baked`。
`ready` = 鍛造階段已完成，等建造階段處理；`baked` = `game/asset.json` 比所有來源新。

## plan/spaces/&lt;space&gt;.json

```jsonc
{
  "id": "cabin_main",
  "summary": "一句話：這個空間是什麼、誰在用、什麼狀態",
  "where": "cabin 一樓，南側門進入",
  "size": [6, 5, 2.6],
  "concepts": [
    {"file": "concepts/cabin_main/concept_2.png",
     "takeaways": "桌子靠窗、窗台上擺滿瓶罐；左牆整面掛鉤掛著外套與工具；地上有編織地毯；光從窗戶斜進來"}
  ],
  "concept_picks": [2],
  "zones": [
    {"name": "desk corner", "items": ["pine_desk", "oil_lamp_brass", "ledger_book", "ink_bottle"], "notes": "..."}
  ],
  "items": ["pine_desk", "oil_lamp_brass", "..."],
  "status": "planned | blocked_out | dressed | reviewed",
  "notes_for_next": "給下一個 agent：哪些還沒做、哪裡覺得空、下一輪想加什麼"
}
```

## build/architecture.json

```jsonc
{
  "defaults": {"bevel": 0.012},
  "buildings": [
    {"id": "cabin", "origin": [0, 0, 0], "yaw": 0, "ground_z": 0.0, "space": "cabin",
     "parts": [
       {"type": "foundation", "poly": [[-0.2,-0.2],[6.2,-0.2],[6.2,5.2],[-0.2,5.2]], "z0": -0.3, "height": 0.8,
        "surface": "stone_river", "mortar": "mortar_lime"},
       {"type": "logwall", "path": [[0,0],[6,0]], "z": 0.5, "height": 2.6, "log_d": 0.28, "surface": "logs_pine",
        "end_surface": "log_end", "chink": "chinking",
        "openings": [{"at": 3.0, "width": 1.0, "height": 2.0, "sill": 0.05, "kind": "door"}]},
       {"type": "roof", "kind": "gable", "rect": [0,0,6,5], "ridge": "x", "eave_z": 3.4, "pitch": 38,
        "overhang": 0.6, "surface": "shingles_cedar", "shingles": {"row": 0.24}, "rafter_tails": {"every": 0.6},
        "gable_fill": {"surface": "boards_weathered"}}
     ]}
  ]
}
```

部件類型：`wall`、`logwall`、`slab`、`roof`、`beam`、`stairs`、`railing`、`chimney`、`foundation`、`chunks`、`pipe`、`box`。
每個參數見 [blender/build_arch.py](../blender/build_arch.py) 開頭。牆的 `path` 逆時針（從上看），右手邊是外側。
`surface` 是 library 裡 `kind: surface` 的 id；玻璃固定叫 `glass`。`eave_z` 要比牆頂高至少半根圓木，否則圓木端會穿出屋頂。

## build/terrain.json

見 [tools/terrain.py](../tools/terrain.py) 開頭。重點：`size`、`res`、`base`、`ridges`、`carve`、`flatten`、`paths`、
`layers`（r / g / b / a 四個 surface id：草、泥土、岩石、林地）、`foliage`（每層：`items` 權重、`density`、`min_dist`、
`area`、`noise`、`slope`、`avoid`、`scale`、`align`、`sink`、`cull`、`shadows`）。

## build/scene.json

見 [tools/scene.py](../tools/scene.py) 開頭。重點：

```jsonc
{
  "instances": [
    {"name": "desk_A", "item": "pine_desk", "pos": [1.6, 4.3, 0.55], "yaw": 180, "space": "cabin_main"},
    {"name": "lamp_A", "item": "oil_lamp_brass", "on": "desk_A", "at": [0.4, 0.1], "yaw": 20},
    {"name": "bench_1", "item": "log_bench", "pos": [8, -4], "yaw": 30}               // 2 個數字 = 貼地形
  ],
  "arrays":  [{"name": "books_shelf2", "items": {"book_red": 2, "book_blue": 1}, "on": "shelf_A", "level": 2,
               "from": [-0.4, 0], "to": [0.4, 0], "count": 11, "jitter": {"yaw": 5}}],
  "scatter": [{"items": {"pine_cone": 1, "twig": 2}, "rect": [-6, -9, 10, -3], "count": 25, "min_dist": 0.5}],
  "decals":  [{"decal": "soot_mark", "pos": [6.2, 2.5, 3.2], "normal": [1, 0, 0]}],
  "decal_rules": [{"decals": ["water_streak"], "anchors": ["under_window"], "chance": 1.0},
                  {"decals": ["moss_patch", "mud_splash"], "anchors": ["wall_base", "wall_corner"], "every": 1.2}],
  "lights":  [{"type": "point", "pos": [1.9, 4.4, 1.45], "lumens": 250, "kelvin": 2000, "radius": 6, "shadows": true,
               "space": "cabin_main"}],
  "lighting": {"gi": "dynamic", "sun": {"dir": [-0.5, 0.4, -0.45], "lux": 25000, "kelvin": 4200}, "sky": true,
               "fog": {"density": 0.025, "height_falloff": 0.15, "volumetric": true},
               "post": {"exposure": [3, 14], "bloom": 0.35, "vignette": 0.25, "saturation": 0.95}},
  "cameras": [{"id": "cabin_main_door", "pos": [3, 0.8, 2.15], "look": [3, 4.5, 1.4], "fov": 75, "space": "cabin_main"}],
  "player_start": {"pos": [3, -6], "yaw": 90}
}
```

- `on` 的父物件尚未烘焙時，用 item 的 `size` 推算高度；烘焙後自動改用真實包圍盒。
- 尚未烘焙的 item 會以洋紅色 `TEMP_<item>` 方塊佔位；下一輪 `engine` 自動換成模型。

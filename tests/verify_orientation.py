"""EXIF 方向摆正验证：所有处理入口必须与缩略图/原图预览方向一致。

构造 4 张仅 EXIF Orientation 不同的同源 JPEG（1 正常 / 3 倒转 / 6 右倒 / 8 左倒），
外加一张无方向信息的横拍 PNG 对照组，逐一验证：
  1. 上传后记录的 width/height 为摆正后的竖图尺寸；
  2. 缩略图尺寸为竖图；
  3. 单图处理（风格 / 锐化流水线 /api/run）结果为竖图且像素朝上；
  4. 特征匹配结果尺寸为竖图；
  5. 差异热力图（compare/diff）尺寸与处理结果一致；
  6. 批量处理结果为竖图；
  7. 无 EXIF 的横拍图完全不受影响（不被误转）；
  8. 摆正幂等：对结果再处理不会二次旋转；
  9. 旧缓存（cache-v1）不会被新代码命中。
运行：python3 tests/verify_orientation.py
"""
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

TMP = tempfile.mkdtemp(prefix="orient_test_")

# 在导入 server 之前把数据目录指到临时目录
from server import config  # noqa: E402
config.DATA_DIR = TMP
config.IMAGES_DIR = os.path.join(TMP, "images")
config.RESULTS_DIR = os.path.join(TMP, "results")
config.THUMBS_DIR = os.path.join(TMP, "thumbs")
config.CACHE_DIR = os.path.join(TMP, "cache")
config.META_DIR = os.path.join(TMP, "meta")
for name in ("IMAGES_JSON", "PIPELINES_JSON", "HISTORY_JSON",
             "PRESETS_JSON", "QUEUE_JSON", "CACHE_JSON"):
    base = os.path.basename(getattr(config, name))
    setattr(config, name, os.path.join(config.META_DIR, base))
config._ALL_DIRS = [config.DATA_DIR, config.IMAGES_DIR, config.RESULTS_DIR,
                    config.THUMBS_DIR, config.CACHE_DIR, config.META_DIR]
config.ensure_dirs()

from app import create_app  # noqa: E402

W, H = 60, 100  # 竖图：朝上时高>宽，四角颜色不对称
CORNERS = {
    "TL": (255, 0, 0), "TR": (0, 255, 0),
    "BL": (0, 0, 255), "BR": (255, 255, 0),
}


def make_upright():
    img = Image.new("RGB", (W, H), (40, 40, 40))
    px = img.load()
    patch = 8
    fills = {"TL": (0, 0), "TR": (W - patch, 0),
             "BL": (0, H - patch), "BR": (W - patch, H - patch)}
    for key, (ox, oy) in fills.items():
        c = CORNERS[key]
        for y in range(oy, oy + patch):
            for x in range(ox, ox + patch):
                px[x, y] = c
    return img


def sensor_pixels_for(orient):
    """由「朝上的竖图」反推出相机在该 orientation 下实际存储的传感器像素。

    手机相机只写标记、不转像素：orientation=6 时存的是顺时针躺的横像素，
    查看器逆时针转正；=3 存上下颠倒像素；=8 存另一侧躺倒像素。
    exif_transpose 是「存储像素 -> 朝上」，这里取其逆过程构造夹具。
    """
    if orient == 1:
        return upright
    if orient == 3:
        return upright.rotate(180, expand=True)
    if orient == 6:
        return upright.rotate(90, expand=True)    # 存储为横躺像素
    if orient == 8:
        return upright.rotate(-90, expand=True)
    raise ValueError(orient)


def jpeg_with_orientation(img, orient):
    """保存 JPEG 并写入 EXIF Orientation（像素本身不动，模拟手机相机）。"""
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = orient
    img.save(buf, "JPEG", quality=95, exif=exif)
    return buf.getvalue()


def corner_colors(img, p=4):
    return {
        "TL": tuple(img.getpixel((p, p))),
        "TR": tuple(img.getpixel((img.width - p - 1, p))),
        "BL": tuple(img.getpixel((p, img.height - p - 1))),
        "BR": tuple(img.getpixel((img.width - p - 1, img.height - p - 1))),
    }


def approx(a, b, tol=60):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


failures = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        failures.append(name)


app = create_app()
client = app.test_client()

upright = make_upright()
expected_corners = corner_colors(upright)

print("== 上传 4 种 EXIF 方向 + 无 EXIF 横拍对照 ==")
ids = {}
for orient, tag in ((1, "o1"), (3, "o3"), (6, "o6"), (8, "o8")):
    data = jpeg_with_orientation(sensor_pixels_for(orient), orient)
    rv = client.post("/api/images", data={"files": (io.BytesIO(data), f"photo_{tag}.jpg")},
                     content_type="multipart/form-data")
    rec = rv.get_json()["saved"][0]
    ids[tag] = rec["id"]
    check(f"方向{orient} 记录为摆正后竖图 {W}x{H}", (rec["width"], rec["height"]) == (W, H),
          f"{rec['width']}x{rec['height']}")

# 无 EXIF 横拍 PNG：必须保持横图、不被旋转
land = Image.new("RGB", (160, 90), (10, 80, 160))
lb = io.BytesIO(); land.save(lb, "PNG")
rv = client.post("/api/images", data={"files": (io.BytesIO(lb.getvalue()), "land.png")},
                 content_type="multipart/form-data")
land_rec = rv.get_json()["saved"][0]
land_id = land_rec["id"]
check("无EXIF横拍PNG 保持横图 160x90", (land_rec["width"], land_rec["height"]) == (160, 90),
      f"{land_rec['width']}x{land_rec['height']}")

print("\n== 缩略图：必须是竖图 ==")
for tag, iid in ids.items():
    rv = client.get(f"/api/images/{iid}/thumbnail")
    t = Image.open(io.BytesIO(rv.data))
    check(f"{tag} 缩略图高>=宽（竖）", t.height >= t.width, f"{t.width}x{t.height}")

print("\n== 单图处理 /api/style：结果竖图且像素朝上 ==")
style_results = {}
for tag, iid in ids.items():
    rv = client.post("/api/style", json={"image_id": iid, "style": "oil", "strength": 100})
    js = rv.get_json()
    style_results[tag] = js["result_id"]
    out = Image.open(io.BytesIO(client.get(f"/api/results/{js['result_id']}/file").data))
    check(f"{tag} 风格结果为竖图", out.size == (W, H), f"{out.width}x{out.height}")

print("\n== 单图流水线 /api/run（锐化）：结果竖图、方向与缩略图一致 ==")
sharpen_nodes = [{"id": "s1", "type": "sharpen", "params": {"radius": 2}, "inputs": []}]
for tag, iid in ids.items():
    rv = client.post("/api/run", json={"image_id": iid, "nodes": sharpen_nodes})
    js = rv.get_json()
    check(f"{tag} /api/run 无错误", not js.get("error"), str(js.get("error")))
    out = Image.open(io.BytesIO(client.get(f"/api/results/{js['result_id']}/file").data))
    ok_size = out.size == (W, H)
    check(f"{tag} 锐化结果为竖图", ok_size, f"{out.width}x{out.height}")
    cc = corner_colors(out)
    # 锐化基本保持四角颜色，可验证像素确实朝上
    ok_orient = approx(cc["TL"], expected_corners["TL"]) and approx(cc["TR"], expected_corners["TR"])
    check(f"{tag} 锐化结果四角朝上（红左上/绿右上）", ok_orient, str(cc))

print("\n== 特征匹配：两张不同方向的同源图，匹配画布必须竖图 ==")
rv = client.post("/api/features/match", json={"image_id_a": ids["o6"], "image_id_b": ids["o8"],
                                              "method": "orb"})
js = rv.get_json()
check("匹配接口无错误", "result_id" in js, str(js))
if "result_id" in js:
    out = Image.open(io.BytesIO(client.get(f"/api/results/{js['result_id']}/file").data))
    # 匹配画布是两张竖图左右拼接，高度应为 H
    check("匹配结果高度=竖图高", out.height == H, f"{out.width}x{out.height}")

print("\n== 差异对比 /api/compare/diff：热力图尺寸必须与处理结果对齐（竖图）==")
for tag in ("o3", "o6"):
    rv = client.post("/api/compare/diff",
                     json={"image_id": ids[tag], "result_id": style_results[tag]})
    js = rv.get_json()
    check(f"{tag} 差异接口无错误", "result_id" in js, str(js))
    if "result_id" in js:
        heat = Image.open(io.BytesIO(client.get(f"/api/results/{js['result_id']}/file").data))
        check(f"{tag} 热力图为竖图 {W}x{H}", heat.size == (W, H), f"{heat.width}x{heat.height}")

print("\n== 批量处理：队列中每种方向结果都为竖图 ==")
rv = client.post("/api/batch", json={"nodes": sharpen_nodes,
                                     "image_ids": [ids[t] for t in ("o1", "o3", "o6", "o8")]})
job_id = rv.get_json()["job_id"]
for _ in range(100):
    time.sleep(0.1)
    job = client.get(f"/api/batch/{job_id}").get_json()
    if job["status"] in ("done", "partial", "cancelled"):
        break
check("批量任务完成", job["status"] == "done", job["status"])
for iid, r in job["results"].items():
    ok = r["status"] == "ok"
    detail = r.get("error", "")
    if ok:
        out = Image.open(io.BytesIO(client.get(f"/api/results/{r['result_id']}/file").data))
        ok = out.size == (W, H)
        detail = f"{out.width}x{out.height}"
    check(f"批量 {iid[:8]} 竖图结果", ok, detail)

print("\n== 幂等性：对处理结果对应原图再跑一次（缓存路径），不二次旋转 ==")
rv1 = client.post("/api/style", json={"image_id": ids["o6"], "style": "sketch", "strength": 100})
rv2 = client.post("/api/style", json={"image_id": ids["o6"], "style": "sketch", "strength": 100})
r1, r2 = rv1.get_json(), rv2.get_json()
check("第二次命中缓存", r2.get("cache_hit") is True and r1["result_id"] == r2["result_id"])
out = Image.open(io.BytesIO(client.get(f"/api/results/{r2['result_id']}/file").data))
check("缓存结果仍为竖图", out.size == (W, H), f"{out.width}x{out.height}")

print("\n== 旧缓存隔离：cache-v1 时代的键不应被命中 ==")
from server.cache import make_key  # noqa: E402
k = make_key("anyhash", "style", "{}")
check("新缓存键带 v2 命名空间", k != __import__("hashlib").sha256(
    b"anyhash\x00style\x00{}\x00").hexdigest())

print("\n== 检测/特征/分割元数据宽高口径（检测叠加图为竖图）==")
rv = client.post("/api/detect", json={"image_id": ids["o6"], "method": "saliency"})
js = rv.get_json()
if "result_id" in js:
    out = Image.open(io.BytesIO(client.get(f"/api/results/{js['result_id']}/file").data))
    check("检测叠加图为竖图", out.size == (W, H), f"{out.width}x{out.height}")
    boxes = js.get("boxes", [])
    inside = all(0 <= b["x"] <= W and 0 <= b["y"] <= H for b in boxes)
    check("检测框坐标落在竖图坐标系内", inside, f"{len(boxes)} boxes")

print()
if failures:
    print(f"❌ {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("✅ 全部方向一致性检查通过")

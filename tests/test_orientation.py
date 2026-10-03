"""验证所有处理入口统一按 EXIF Orientation 摆正。

构造：一张明确的竖图（200x400，顶部画红条、底部画蓝条），
分别嵌入 EXIF Orientation=6（顺时针 90° 存储）和 =8（逆时针 90° 存储）
以及 =1（无旋转），走：上传/元数据、缩略图、单图运算、流水线/单图处理、
特征匹配、差异对比、批处理 全链路，断言：

1. 元数据尺寸与浏览器所见（摆正后）一致（竖图 200x400）；
2. 缩略图是竖的；
3. 任意处理结果都是竖的，且与「无旋转版本」处理结果像素一致；
4. 差异对比结果尺寸与原图一致（方向口径相同）；
5. 无 EXIF 的横拍照片不受影响。

运行：python tests/test_orientation.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from server import config  # noqa: E402
import app as app_module  # noqa: E402


W, H = 200, 400  # 竖图：宽 < 高


def make_upright():
    """一张「上红下蓝、文字上下可分」的竖图。"""
    img = Image.new("RGB", (W, H), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, H // 4], fill=(220, 30, 30))        # 顶部红条
    d.rectangle([0, 3 * H // 4, W, H], fill=(30, 60, 220))    # 底部蓝条
    d.ellipse([W // 2 - 20, H // 2 - 20, W // 2 + 20, H // 2 + 20], fill=(20, 180, 60))
    return img


def jpeg_with_orientation(upright: Image.Image, orientation: int):
    """模拟手机相机输出：按 EXIF 方向把像素旋转存储，并写入 Orientation 标签。

    Orientation 6 = 相机顺时针转了 90° 拍摄 => 像素被逆时针旋转存储；
    Orientation 8 = 逆时针转了 90° => 像素被顺时针旋转存储。
    exif_transpose 后都应还原成 upright。
    """
    img = upright.copy()
    if orientation == 6:
        # 标签 6 表示显示时需顺时针 90°（Pillow rotate 正角为逆时针，故 +90）
        pixels = img.rotate(90, expand=True)
    elif orientation == 8:
        pixels = img.rotate(-90, expand=True)
    elif orientation == 3:
        pixels = img.rotate(180, expand=True)
    else:
        pixels = img
    exif = img.getexif()
    exif[274] = orientation  # 274 = EXIF Orientation tag
    buf = io.BytesIO()
    pixels.save(buf, "JPEG", quality=95, exif=exif.tobytes())
    return buf.getvalue()


def png_landscape():
    """无 EXIF 的横拍照片（PNG 不带方向信息）。"""
    img = Image.new("RGB", (480, 270), (250, 250, 200))
    ImageDraw.Draw(img).rectangle([10, 10, 200, 200], fill=(40, 120, 40))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main():
    flask_app = app_module.create_app()
    client = flask_app.test_client()

    print("== 上传三种方向的同一张竖图 + 一张无 EXIF 横图 ==")
    upright = make_upright()
    uploads = {}
    for tag, ori in (("up", 1), ("cw90", 6), ("ccw90", 8), ("flip180", 3)):
        data = jpeg_with_orientation(upright, ori)
        resp = client.post("/api/images", data={"files": (io.BytesIO(data), f"{tag}.jpg")},
                           content_type="multipart/form-data")
        rec = resp.get_json()["saved"][0]
        uploads[tag] = rec
        check(f"{tag}: 元数据尺寸为摆正后的竖图 {W}x{H}",
              (rec["width"], rec["height"]) == (W, H),
              f"got {rec['width']}x{rec['height']}")

    resp = client.post("/api/images", data={"files": (io.BytesIO(png_landscape()), "land.png")},
                       content_type="multipart/form-data")
    land = resp.get_json()["saved"][0]
    check("无 EXIF 横图尺寸不变 480x270", (land["width"], land["height"]) == (480, 270),
          f"got {land['width']}x{land['height']}")

    print("== 缩略图方向 ==")
    from PIL import Image as PILImage
    for tag, rec in uploads.items():
        resp = client.get(f"/api/images/{rec['id']}/thumbnail")
        thumb = PILImage.open(io.BytesIO(resp.data))
        check(f"{tag}: 缩略图竖向 (h>w)", thumb.size[1] > thumb.size[0], f"size={thumb.size}")

    nodes = [{"id": "n1", "type": "brightness", "params": {"amount": 25}, "inputs": []}]

    print("== 单图处理（流水线 /run）结果方向 ==")
    result_pixels = {}
    for tag, rec in uploads.items():
        resp = client.post("/api/run", json={"image_id": rec["id"], "nodes": nodes})
        rj = resp.get_json()
        check(f"{tag}: /run 成功", not rj.get("error"), str(rj.get("error")))
        out = PILImage.open(io.BytesIO(client.get(rj["file_url"]).data))
        check(f"{tag}: 结果竖图 {W}x{H}", out.size == (W, H), f"got {out.size}")
        result_pixels[tag] = list(out.convert("RGB").getdata())

    print("== 处理结果不仅方向对、而且摆正到同一画面（容差比对） ==")
    # JPEG 有损：不同存储方向的压缩伪影不同，不能要求逐像素相等；
    # 用「上下色带位置」做语义断言，再用平均色差做容差断言。
    def semantically_upright(px_list):
        top = px_list[: W * (H // 8)]
        bottom = px_list[(7 * H // 8) * W:]
        avg = lambda band, rows: sum(p[band] for p in rows) / len(rows)
        return avg(0, top) > 150 and avg(2, top) < 120 and \
               avg(2, bottom) > 150 and avg(0, bottom) < 120

    for tag in ("up", "cw90", "ccw90", "flip180"):
        check(f"{tag}: 结果红条在上、蓝条在下（画面摆正）",
              semantically_upright(result_pixels[tag]))

    def mean_abs_diff(a, b):
        return sum(abs(x - y) for pa, pb in zip(a, b) for x, y in zip(pa, pb)) / (len(a) * 3)

    for tag in ("cw90", "ccw90", "flip180"):
        mad = mean_abs_diff(result_pixels[tag], result_pixels["up"])
        check(f"{tag} 与无旋转版本平均色差 < 3（仅 JPEG 伪影差异）", mad < 3,
              f"mad={mad:.2f}")

    print("== 单图运算（风格/锐化类）/style ==")
    for tag in ("cw90", "ccw90"):
        rec = uploads[tag]
        resp = client.post("/api/style", json={"image_id": rec["id"], "style": "sketch"})
        rj = resp.get_json()
        check(f"{tag}: /style 成功", "result_id" in rj, str(rj))
        out = PILImage.open(io.BytesIO(client.get(f"/api/results/{rj['result_id']}/file").data))
        check(f"{tag}: 风格结果竖图", out.size == (W, H), f"got {out.size}")

    print("== 特征匹配（双图）方向 ==")
    resp = client.post("/api/features/match", json={
        "image_id_a": uploads["up"]["id"], "image_id_b": uploads["cw90"]["id"]})
    rj = resp.get_json()
    check("match 返回结果", "result_id" in rj, str(rj))
    if "result_id" in rj:
        out = PILImage.open(io.BytesIO(client.get(f"/api/results/{rj['result_id']}/file").data))
        check("匹配结果竖图", out.size[1] >= out.size[0] or out.size[0] == W, f"got {out.size}")

    print("== 差异对比（原图 vs 处理结果）方向口径 ==")
    rec = uploads["cw90"]
    run = client.post("/api/run", json={"image_id": rec["id"], "nodes": nodes}).get_json()
    resp = client.post("/api/compare/diff",
                       json={"image_id": rec["id"], "result_id": run["result_id"]})
    dj = resp.get_json()
    check("diff 返回结果", "result_id" in dj, str(dj))
    if "result_id" in dj:
        out = PILImage.open(io.BytesIO(client.get(f"/api/results/{dj['result_id']}/file").data))
        check("diff 热力图与原图同尺寸（方向口径一致）", out.size == (W, H), f"got {out.size}")
        check("diff 指标可计算", "metrics" in dj and dj["metrics"]["mse"] >= 0)

    print("== 批处理方向 ==")
    resp = client.post("/api/batch", json={
        "nodes": nodes,
        "image_ids": [uploads[t]["id"] for t in ("cw90", "ccw90")]})
    job_id = resp.get_json()["job_id"]
    import time
    job = None
    for _ in range(100):
        time.sleep(0.1)
        job = client.get(f"/api/batch/{job_id}").get_json()
        if job["status"] in ("done", "partial", "cancelled"):
            break
    check("批处理完成", job["status"] == "done", f"status={job['status']}")
    for iid, r in job["results"].items():
        out = PILImage.open(io.BytesIO(
            client.get(f"/api/results/{r['result_id']}/file").data))
        check(f"批量结果 {iid[:8]}.. 竖图", out.size == (W, H), f"got {out.size}")

    print("== 无 EXIF 横图处理后仍为横图 ==")
    rj = client.post("/api/run", json={"image_id": land["id"], "nodes": nodes}).get_json()
    out = PILImage.open(io.BytesIO(client.get(rj["file_url"]).data))
    check("横图结果保持 480x270", out.size == (480, 270), f"got {out.size}")

    print()
    if FAILURES:
        print(f"存在 {len(FAILURES)} 项失败 ✗: {FAILURES}")
        sys.exit(1)
    print("全部方向一致性检查通过 ✔")


if __name__ == "__main__":
    main()

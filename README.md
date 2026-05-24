# Chicago-Only Route Planner

FastAPI web app + OpenTripPlanner orchestration for routing inside Chicago with CTA rail highlighted on the map.

## What Changed

- `server.py` now launches the FastAPI app through `uvicorn` without shell-specific wrappers.
- Routing is orchestrated through `app/services/routing.py`, using OTP GraphQL only.
- Static metadata is served from generated local assets in `data/assets/`.
- Frontend lives in `static/index.html` and talks only to backend APIs.
- The only startup entrypoint is `run.py`, shared by macOS and Windows.

## Quick Sta

Nếu bạn vừa tải (clone) dự án này từ GitHub về, máy bạn sẽ chưa có các file dữ liệu khổng lồ (bản đồ, GTFS, OpenTripPlanner) do chúng đã được loại trừ để tối ưu dung lượng GitHub. Bạn chỉ làm các bước sau để tự động tải dữ liệu:

1. Cài đặt các thư viện Python cần thiết:

```bash
python -m pip install -r requirements.txt
```

2. Chạy thiết lập lần đầu (Hệ thống sẽ TỰ ĐỘNG tải OTP, tải bản đồ Chicago OSM, tải dữ liệu tàu CTA và biên dịch bản đồ giao thông cho bạn - cần Internet và mất vài phút):

```bash
python run.py setup
```

Sau khi hoàn tất cài đặt, lệnh khởi động/tắt hằng ngày chung cho cả macOS và Windows:

3. Bật hệ thống:

```bash
python run.py
```

4. Kiem tra trang thai:

```bash
python run.py status
```

5. Tat he thong:

```bash
python run.py stop
```

## Cách Dùng

- Mở `http://127.0.0.1:8000`
- Chọn `Đi bộ + tàu CTA`
- Chọn giờ khởi hành
- Ở chế độ `Chọn điểm đầu/cuối`, click 2 điểm nằm trong phạm vi Chicago
- Nếu cần nhiều điểm dừng, chuyển sang `Thêm điểm dừng`
- Nếu cần cấm đi qua một đoạn đường, chuyển sang `Vẽ đoạn đường cấm` rồi click 2 điểm để tạo đoạn tránh
- Chọn `Giữ đúng thứ tự` hoặc `Tối ưu thứ tự` cho các điểm dừng
- Nhấn `Tính lộ trình tốt nhất`
- Kết quả sẽ hiển thị tổng thời gian, tổng quãng đường, phần cộng thêm do ùn tắc/thời tiết, các cảnh báo khu vực rủi ro, và chi tiết từng chặng

## API

- `GET /api/meta/boundary`
- `GET /api/meta/rail`
- `GET /api/meta/context`
- `GET /api/route?from=41.88,-87.63&to=41.79,-87.60&profile=walk&depart_at=2026-04-07T09:30`
- `POST /api/route`

Ví dụ `POST /api/route`:

```json
{
  "origin": { "lat": 41.88, "lon": -87.63 },
  "destination": { "lat": 41.79, "lon": -87.60 },
  "profile": "walk",
  "depart_at": "2026-04-07T08:15",
  "stops": [
    { "lat": 41.87, "lon": -87.65 },
    { "lat": 41.84, "lon": -87.67 }
  ],
  "stop_order_mode": "optimize",
  "blocked_segments": [
    {
      "start": { "lat": 41.88, "lon": -87.63 },
      "end": { "lat": 41.881, "lon": -87.631 },
      "label": "Đoạn đang thi công",
      "buffer_m": 45
    }
  ]
}
```

`depart_at` is interpreted as Chicago local time if no timezone offset is supplied.

## Notes

- The app rejects points outside the Chicago boundary.
- The direct City of Chicago boundary export is blocked from this environment, so `scripts/prepare_assets.py` falls back to a Census-derived Chicago boundary file while preserving the official URL in metadata.
- `profile=walk` compares `đi bộ toàn tuyến` against `đi bộ + tàu CTA` and picks the faster valid option.

- Contextual scoring uses local static metadata in `data/assets/contextual_factors.json` to model average congestion by time bucket and to warn about flood-prone or snow-prone corridors.
- `python run.py setup` accepts any `.osm.pbf` placed in `otp/runtime/`; a Chicago-only clip is best, but an Illinois extract also works because the backend rejects routes that leave the Chicago boundary.

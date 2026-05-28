# Chicago Route Planner

Ứng dụng bản đồ hướng dẫn chỉ đường khu vực Chicago, được xây dựng với cấu trúc **100% tự code thuật toán** (A* và Genetic Algorithm) bằng **C++ Backend** để đạt hiệu năng tối ưu, kết hợp với **FastAPI** phục vụ dữ liệu và giao diện web.

## Kiến trúc Hệ thống

Dự án được chia thành 2 thành phần chính:
1. **Frontend (Giao diện Web):** Nằm trong thư mục `frontend/`. Chứa các file HTML/CSS/JS (sử dụng thư viện Leaflet.js) để hiển thị bản đồ trực quan, tiếp nhận thao tác của người dùng.
2. **Backend (API & Thuật toán):**
   - **Lõi C++ (`backend/algorithms/`):** Tự triển khai thuật toán A* (để tìm đường ngắn nhất trên đồ thị đường phố) và Genetic Algorithm - GA (để giải bài toán TSP đa điểm dừng). Biên dịch thành file thực thi `router`.
   - **FastAPI (`backend/api/`):** Đóng vai trò là Web Server. Nhận Request từ Frontend, tiền xử lý tọa độ, gọi C++ Engine qua Subprocess để lấy lộ trình, rồi cộng thêm các trọng số ngoại cảnh (Kẹt xe, ngập lụt) trước khi trả về JSON cho Frontend.

Cấu trúc thư mục:
```text
chicago_transit/
├── frontend/               # Giao diện web (HTML/JS/CSS)
├── backend/                # Toàn bộ mã nguồn xử lý
│   ├── api/                # FastAPI (Controllers, Models, Services)
│   ├── algorithms/         # Mã nguồn thuật toán C++ (Astar, GA, main)
│   ├── scripts/            # Các tập lệnh (Build đồ thị từ bản đồ)
│   └── server.py           # File chạy web server Uvicorn
├── data/                   # Chứa file đồ thị (.txt) và dữ liệu tĩnh (.json)
├── run.py                  # Lệnh khởi động chung
└── requirements.txt        # Thư viện Python
```

## Hướng dẫn Cài đặt & Chạy

### 1. Cài đặt thư viện Python
Máy tính của bạn cần cài sẵn Python 3 và trình biên dịch C++ (ví dụ `g++` hoặc `clang++` thường có sẵn trên macOS/Linux). Cài đặt thư viện:

```bash
pip install -r requirements.txt
pip install osmnx networkx  # Dùng để trích xuất đồ thị ban đầu
```

### 2. Biên dịch C++ và Khởi tạo Đồ thị (Chỉ chạy 1 lần)
Chạy lệnh sau để hệ thống tự động tải dữ liệu bản đồ Chicago, trích xuất đồ thị (`data_graph.txt`) và biên dịch lõi thuật toán C++:

```bash
python run.py setup
```
*(Lưu ý: Quá trình trích xuất đồ thị bằng osmnx có thể mất khoảng 1-2 phút tùy tốc độ mạng).*

### 3. Khởi động ứng dụng (Dùng hằng ngày)
Khởi động máy chủ web:

```bash
python run.py start
```
Truy cập địa chỉ `http://127.0.0.1:8000` trên trình duyệt để sử dụng bản đồ.

## Các Tính năng Chính
- Tìm đường đi bộ và đi qua mạng lưới tàu điện CTA.
- Hỗ trợ tối ưu hóa **Nhiều điểm dừng (Multi-stops)** bằng giải thuật Di truyền (GA).
- Tự động cảnh báo ngập lụt cục bộ hoặc kẹt xe và cộng thêm thời gian phạt vào lộ trình.
- Khả năng **Vẽ đoạn đường cấm** trực tiếp trên bản đồ để hệ thống tự tìm đường vòng.

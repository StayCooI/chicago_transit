#!/usr/bin/env python3
import os
import networkx as nx
import math
import json
from pathlib import Path

# Thử import osmnx, nếu lỗi thì thoát an toàn
try:
    import osmnx as ox
except ImportError:
    print("Vui lòng cài đặt osmnx: pip install osmnx")
    exit(1)

ROOT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT_DIR / "data" / "assets"
GRAPH_FILE = ASSETS_DIR / "data_graph.txt"
RAIL_STATIONS_FILE = ASSETS_DIR / "rail_stations.json"

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000  # meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def build_graph():
    print("Đang tải dữ liệu mạng lưới đường phố Chicago (chỉ lấy đường chính để tối ưu)...")
    # Lấy dữ liệu đường sá. network_type='drive' sẽ nhẹ hơn 'walk' rất nhiều, phù hợp để thử nghiệm A* chạy nhanh.
    # Trong môi trường thực tế, nếu RAM lớn có thể đổi thành 'all'.
    place_name = "Chicago, Illinois, USA"
    G = ox.graph_from_place(place_name, network_type='drive', simplify=True)
    
    # Lấy thông tin các node và edge
    nodes = list(G.nodes(data=True))
    edges = list(G.edges(data=True))

    # Gán ID tuần tự cho node từ 0 đến N-1
    node_mapping = {}
    for idx, (node_id, data) in enumerate(nodes):
        node_mapping[node_id] = idx

    # Đọc thêm các ga tàu CTA nếu có
    rail_nodes = []
    rail_edges = []
    if RAIL_STATIONS_FILE.exists():
        try:
            with open(RAIL_STATIONS_FILE, 'r', encoding='utf-8') as f:
                stations_data = json.load(f)
                
            # stations_data thường là một dict các ga, hoặc FeatureCollection. 
            # Ta cần kiểm tra định dạng. Giả sử ta trích xuất được tọa độ:
            # (Đoạn này có thể cần điều chỉnh tùy theo cấu trúc thực tế của rail_stations.json)
            # Tạm thời để trống logic nối ga tàu hoặc xử lý sau nếu cấu trúc phức tạp.
            pass
        except Exception as e:
            print(f"Lỗi khi đọc rail_stations: {e}")

    total_nodes = len(nodes) + len(rail_nodes)
    
    # Lọc các edge hợp lệ
    valid_edges = []
    for u, v, data in edges:
        if u in node_mapping and v in node_mapping:
            weight = data.get('length', 0.0)
            if weight == 0.0:
                # Tính haversine nếu osmnx không trả về length
                lat1, lon1 = G.nodes[u]['y'], G.nodes[u]['x']
                lat2, lon2 = G.nodes[v]['y'], G.nodes[v]['x']
                weight = haversine(lat1, lon1, lat2, lon2)
            valid_edges.append((node_mapping[u], node_mapping[v], weight))

    total_edges = len(valid_edges) + len(rail_edges)

    print(f"Đã trích xuất xong: {total_nodes} nodes, {total_edges} edges.")
    print(f"Đang lưu vào {GRAPH_FILE} ...")
    
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    with open(GRAPH_FILE, 'w') as f:
        # Dòng 1: N M
        f.write(f"{total_nodes} {total_edges}\n")
        
        # In các Node: ID Lat Lon
        for node_id, data in nodes:
            idx = node_mapping[node_id]
            f.write(f"{idx} {data['y']:.6f} {data['x']:.6f}\n")
            
        # In các Edge: u v weight (distance)
        for u, v, weight in valid_edges:
            f.write(f"{u} {v} {weight:.2f}\n")

    print("Hoàn tất!")

if __name__ == "__main__":
    build_graph()

#include "Astar.h"
#include "GA.h"
#include <iostream>
#include <vector>
#include <string>

using namespace std;

int main(int argc, char** argv) {
    if (argc < 2) {
        cerr << "Usage: " << argv[0] << " <graph_file>" << endl;
        return 1;
    }

    Graph graph;
    if (!graph.loadFromFile(argv[1])) {
        cerr << "Failed to load graph from " << argv[1] << endl;
        return 1;
    }

    // Read from stdin
    double s_lat, s_lon, t_lat, t_lon;
    if (!(cin >> s_lat >> s_lon >> t_lat >> t_lon)) return 0;

    int num_stops;
    if (!(cin >> num_stops)) return 0;

    vector<pair<double, double>> stops(num_stops);
    for (int i = 0; i < num_stops; ++i) {
        cin >> stops[i].first >> stops[i].second;
    }

    int num_blocked;
    if (!(cin >> num_blocked)) return 0;

    vector<BlockedSegment> blocked_segments(num_blocked);
    for (int i = 0; i < num_blocked; ++i) {
        cin >> blocked_segments[i].lat1 >> blocked_segments[i].lon1
            >> blocked_segments[i].lat2 >> blocked_segments[i].lon2
            >> blocked_segments[i].buffer_m;
    }
    graph.applyBlockedSegments(blocked_segments);

    int start_node = graph.findNearestNode(s_lat, s_lon);
    int target_node = graph.findNearestNode(t_lat, t_lon);

    if (start_node == -1 || target_node == -1) {
        cout << "{\"error\": \"Could not find nearest nodes.\"}" << endl;
        return 0;
    }

    if (num_stops == 0) {
        AStarResult res = findPathAStar(graph, start_node, target_node);
        if (!res.found) {
            cout << "{\"error\": \"Path not found.\"}" << endl;
            return 0;
        }

        cout << "{\"total_time\": " << res.total_time 
             << ", \"total_distance\": " << res.total_distance 
             << ", \"path\": [";
        for (size_t i = 0; i < res.path.size(); ++i) {
            const auto& step = res.path[i];
            const auto& node = graph.nodes[step.node_id];
            cout << "{\"lat\": " << node.lat << ", \"lon\": " << node.lon << ", \"type\": " << step.type_from_prev << "}";
            if (i < res.path.size() - 1) cout << ",";
        }
        cout << "]}" << endl;
        return 0;
    }

    // With stops, we need to build distance matrix and use GA
    vector<int> stop_nodes(num_stops);
    for (int i = 0; i < num_stops; ++i) {
        stop_nodes[i] = graph.findNearestNode(stops[i].first, stops[i].second);
    }

    vector<int> all_nodes = {start_node};
    for(int s : stop_nodes) all_nodes.push_back(s);
    all_nodes.push_back(target_node);

    int n_all = all_nodes.size();
    vector<vector<double>> distMatrix(num_stops, vector<double>(num_stops, 0.0));
    
    // We actually need dist between S -> stops, stops -> T, and stops -> stops.
    // GA handles stops order. S is always first, T is always last.
    // The GA fitness = dist(S, C[0]) + dist(C[0], C[1]) ... + dist(C[n-1], T).
    
    // To implement exactly like the BTL, let's just precalculate A* for all pairs.
    vector<vector<AStarResult>> allPairs(n_all, vector<AStarResult>(n_all));
    for (int i = 0; i < n_all; ++i) {
        for (int j = 0; j < n_all; ++j) {
            if (i == j) {
                allPairs[i][j].total_time = 0;
                allPairs[i][j].total_distance = 0;
                allPairs[i][j].path = {{all_nodes[i], -1, 0.0}};
            } else {
                allPairs[i][j] = findPathAStar(graph, all_nodes[i], all_nodes[j]);
            }
        }
    }

    // Modify GA to include S and T in fitness.
    // Since GA.h currently doesn't, we will do a simple brute force if num_stops <= 8
    // Otherwise, we could just greedily sort them for now or write a full GA inline for the wrapper.
    // Wait, let's use the provided GA but we need a custom fitness that includes S and T.
    
    vector<int> best_order;
    double best_cost = numeric_limits<double>::infinity();

    if (num_stops <= 8) {
        // Exact
        vector<int> perm(num_stops);
        for(int i=0; i<num_stops; ++i) perm[i] = i;
        do {
            double cost = allPairs[0][perm[0] + 1].total_time;
            for(int i=0; i<num_stops-1; ++i) {
                cost += allPairs[perm[i] + 1][perm[i+1] + 1].total_time;
            }
            cost += allPairs[perm[num_stops-1] + 1][n_all-1].total_time;
            
            if (cost < best_cost) {
                best_cost = cost;
                best_order = perm;
            }
        } while(next_permutation(perm.begin(), perm.end()));
    } else {
        // Fallback to greedy
        vector<bool> visited(num_stops, false);
        int current = 0; // S
        for(int step=0; step<num_stops; ++step) {
            int best_next = -1;
            double min_d = numeric_limits<double>::infinity();
            for(int i=0; i<num_stops; ++i) {
                if(!visited[i]) {
                    if(allPairs[current][i+1].total_time < min_d) {
                        min_d = allPairs[current][i+1].total_time;
                        best_next = i;
                    }
                }
            }
            best_order.push_back(best_next);
            visited[best_next] = true;
            current = best_next + 1;
        }
        
        best_cost = allPairs[0][best_order[0] + 1].total_time;
        for(int i=0; i<num_stops-1; ++i) {
            best_cost += allPairs[best_order[i] + 1][best_order[i+1] + 1].total_time;
        }
        best_cost += allPairs[best_order[num_stops-1] + 1][n_all-1].total_time;
    }

    // Calculate total distance for the stitched path
    double total_distance = 0;
    total_distance += allPairs[0][best_order[0] + 1].total_distance;
    for(int i=0; i<num_stops-1; ++i) {
        total_distance += allPairs[best_order[i] + 1][best_order[i+1] + 1].total_distance;
    }
    total_distance += allPairs[best_order[num_stops-1] + 1][n_all-1].total_distance;

    cout << "{\"total_time\": " << best_cost << ", \"total_distance\": " << total_distance << ", \"path\": [";
    
    // Stitch path
    vector<PathStep> full_path;
    int curr = 0;
    for(int stop_idx : best_order) {
        int nxt = stop_idx + 1;
        auto p = allPairs[curr][nxt].path;
        if(full_path.empty()) full_path = p;
        else full_path.insert(full_path.end(), p.begin() + 1, p.end());
        curr = nxt;
    }
    auto p_end = allPairs[curr][n_all-1].path;
    if(full_path.empty()) full_path = p_end;
    else full_path.insert(full_path.end(), p_end.begin() + 1, p_end.end());

    for (size_t i = 0; i < full_path.size(); ++i) {
        const auto& step = full_path[i];
        const auto& node = graph.nodes[step.node_id];
        cout << "{\"lat\": " << node.lat << ", \"lon\": " << node.lon << ", \"type\": " << step.type_from_prev << "}";
        if (i < full_path.size() - 1) cout << ",";
    }
    cout << "]}" << endl;

    return 0;
}

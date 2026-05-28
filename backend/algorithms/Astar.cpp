#include "Astar.h"
#include <iostream>
#include <fstream>
#include <sstream>
#include <cmath>
#include <queue>
#include <limits>

using namespace std;

// Haversine distance
double haversine(double lat1, double lon1, double lat2, double lon2) {
    const double R = 6371000.0; // meters
    double p1 = lat1 * M_PI / 180.0;
    double p2 = lat2 * M_PI / 180.0;
    double dp = (lat2 - lat1) * M_PI / 180.0;
    double dl = (lon2 - lon1) * M_PI / 180.0;

    double a = sin(dp / 2) * sin(dp / 2) +
               cos(p1) * cos(p2) *
               sin(dl / 2) * sin(dl / 2);
    double c = 2 * atan2(sqrt(a), sqrt(1 - a));

    return R * c;
}

bool Graph::loadFromFile(const string& filename) {
    ifstream infile(filename);
    if (!infile.is_open()) return false;

    int num_nodes, num_edges;
    if (!(infile >> num_nodes >> num_edges)) return false;

    nodes.resize(num_nodes);
    adj.resize(num_nodes);

    for (int i = 0; i < num_nodes; ++i) {
        int id;
        double lat, lon;
        infile >> id >> lat >> lon;
        nodes[id] = {id, lat, lon};
    }

    for (int i = 0; i < num_edges; ++i) {
        int u, v;
        double weight;
        infile >> u >> v >> weight;
        adj[u].push_back({v, weight});
        // Assuming bidirectional for simplification unless osmnx output was directed. 
        // Wait, osmnx outputs directed edges if network_type is drive, so it is already correctly handled if we just read it.
    }

    infile.close();
    return true;
}

int Graph::findNearestNode(double lat, double lon) const {
    int best_id = -1;
    double min_dist = numeric_limits<double>::infinity();
    for (const auto& node : nodes) {
        double d = haversine(lat, lon, node.lat, node.lon);
        if (d < min_dist) {
            min_dist = d;
            best_id = node.id;
        }
    }
    return best_id;
}

struct State {
    int u;
    double g;
    double f;
    bool operator>(const State& other) const {
        return f > other.f;
    }
};

AStarResult findPathAStar(const Graph& graph, int start, int target) {
    AStarResult res;
    res.found = false;
    res.total_cost = 0;

    if (start < 0 || start >= graph.nodes.size() || target < 0 || target >= graph.nodes.size()) {
        return res;
    }

    int n = graph.nodes.size();
    vector<double> g(n, numeric_limits<double>::infinity());
    vector<int> parent(n, -1);

    priority_queue<State, vector<State>, greater<State>> pq;

    g[start] = 0;
    double h_start = haversine(graph.nodes[start].lat, graph.nodes[start].lon, 
                               graph.nodes[target].lat, graph.nodes[target].lon);
    pq.push({start, 0, h_start});

    while (!pq.empty()) {
        State current = pq.top();
        pq.pop();

        int u = current.u;

        if (u == target) {
            res.found = true;
            res.total_cost = g[u];
            int curr = target;
            while (curr != -1) {
                res.path.push_back(curr);
                curr = parent[curr];
            }
            // Reverse path
            for(int i=0; i<res.path.size()/2; ++i) {
                swap(res.path[i], res.path[res.path.size() - 1 - i]);
            }
            return res;
        }

        if (current.g > g[u]) continue;

        for (const auto& edge : graph.adj[u]) {
            int v = edge.to;
            double weight = edge.weight;
            if (g[u] + weight < g[v]) {
                g[v] = g[u] + weight;
                parent[v] = u;
                double h = haversine(graph.nodes[v].lat, graph.nodes[v].lon, 
                                     graph.nodes[target].lat, graph.nodes[target].lon);
                pq.push({v, g[v], g[v] + h});
            }
        }
    }

    return res;
}

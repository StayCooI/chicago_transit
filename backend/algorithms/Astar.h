#pragma once

#include <vector>
#include <string>
#include <unordered_map>

struct Node {
    int id;
    double lat;
    double lon;
};

struct Edge {
    int to;
    double weight; // Distance in meters
};

struct AStarResult {
    bool found;
    double total_cost;
    std::vector<int> path;
};

class Graph {
public:
    std::vector<Node> nodes;
    std::vector<std::vector<Edge>> adj;

    bool loadFromFile(const std::string& filename);
    int findNearestNode(double lat, double lon) const;
};

AStarResult findPathAStar(const Graph& graph, int start, int target);

#pragma once

#include <vector>

struct Individual {
    std::vector<int> gene; // Sequence of stops
    double fitness;
};

// Given a distance matrix (N x N) for N stops.
// returns the best permutation of stops (indices 0 to N-1).
std::vector<int> solveTSP_GA(const std::vector<std::vector<double>>& distMatrix, int numGenerations = 100, int popSize = 50);

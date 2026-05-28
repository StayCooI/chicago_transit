#include "GA.h"
#include <algorithm>
#include <random>
#include <iostream>

using namespace std;

// Compute fitness for an individual
double computeFitness(const vector<int>& gene, const vector<vector<double>>& distMatrix) {
    double totalDist = 0;
    for (size_t i = 0; i < gene.size() - 1; ++i) {
        totalDist += distMatrix[gene[i]][gene[i+1]];
    }
    return totalDist;
}

// Order Crossover (OX)
Individual crossoverOX(const Individual& p1, const Individual& p2, mt19937& rng) {
    int n = p1.gene.size();
    Individual child;
    child.gene.assign(n, -1);
    
    if (n <= 1) {
        child.gene = p1.gene;
        return child;
    }

    uniform_int_distribution<int> dist(0, n - 1);
    int start = dist(rng);
    int end = dist(rng);
    if (start > end) swap(start, end);

    vector<bool> inChild(n, false);
    for (int i = start; i <= end; ++i) {
        child.gene[i] = p1.gene[i];
        inChild[child.gene[i]] = true;
    }

    int p2Idx = 0;
    for (int i = 0; i < n; ++i) {
        if (child.gene[i] == -1) {
            while (inChild[p2.gene[p2Idx]]) {
                p2Idx++;
            }
            child.gene[i] = p2.gene[p2Idx];
            inChild[p2.gene[p2Idx]] = true;
        }
    }
    
    return child;
}

// Mutation: Swap two random genes
void mutate(Individual& ind, double mutationRate, mt19937& rng) {
    int n = ind.gene.size();
    if (n <= 1) return;
    uniform_real_distribution<double> realDist(0.0, 1.0);
    uniform_int_distribution<int> intDist(0, n - 1);

    for (int i = 0; i < n; ++i) {
        if (realDist(rng) < mutationRate) {
            int j = intDist(rng);
            swap(ind.gene[i], ind.gene[j]);
        }
    }
}

vector<int> solveTSP_GA(const vector<vector<double>>& distMatrix, int numGenerations, int popSize) {
    int n = distMatrix.size();
    vector<int> bestSequence;
    if (n <= 2) {
        for(int i=0; i<n; ++i) bestSequence.push_back(i);
        return bestSequence;
    }

    random_device rd;
    mt19937 rng(rd());

    vector<Individual> population(popSize);
    for (int i = 0; i < popSize; ++i) {
        population[i].gene.resize(n);
        for (int j = 0; j < n; ++j) population[i].gene[j] = j;
        // Keep start and end fixed? The requirement doesn't state it, 
        // assuming TSP needs all points, and start/end are fixed externally. 
        // Wait, if it's a path through stops, the order of intermediate stops matters. 
        // Here, the distMatrix covers ONLY the intermediate stops! So any permutation is valid.
        shuffle(population[i].gene.begin(), population[i].gene.end(), rng);
        population[i].fitness = computeFitness(population[i].gene, distMatrix);
    }

    double mutationRate = 0.1;

    for (int gen = 0; gen < numGenerations; ++gen) {
        sort(population.begin(), population.end(), [](const Individual& a, const Individual& b) {
            return a.fitness < b.fitness;
        });

        vector<Individual> newPop;
        int eliteCount = max(1, popSize / 5);
        for (int i = 0; i < eliteCount; ++i) {
            newPop.push_back(population[i]);
        }

        uniform_int_distribution<int> parentDist(0, popSize / 2);
        while (newPop.size() < popSize) {
            Individual p1 = population[parentDist(rng)];
            Individual p2 = population[parentDist(rng)];
            Individual child = crossoverOX(p1, p2, rng);
            mutate(child, mutationRate, rng);
            child.fitness = computeFitness(child.gene, distMatrix);
            newPop.push_back(child);
        }
        population = newPop;
    }

    sort(population.begin(), population.end(), [](const Individual& a, const Individual& b) {
        return a.fitness < b.fitness;
    });

    return population[0].gene;
}

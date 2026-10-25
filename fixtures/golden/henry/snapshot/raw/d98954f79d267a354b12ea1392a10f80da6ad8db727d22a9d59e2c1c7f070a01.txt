#include <vector>
#include <iostream>
#include <unordered_set>
#include <queue>
#include <cstdlib>
#include <ctime>
#include <algorithm>
#include <unordered_map>
#include <chrono>

using namespace std;

struct CubeState {
    vector<int> pieces;
    bool valid;

    CubeState() : valid(false) {
        for (int i = 0; i < 24; i++) {
            pieces.push_back(i);
        }
    }

    bool operator==(const CubeState& other) const {
        return pieces == other.pieces;
    }
};

struct CubeStateHash {
    size_t operator()(const CubeState& state) const {
        size_t hash = 0;
        for (int piece : state.pieces) {
            hash ^= std::hash<int>{}(piece) + 0x9e3779b9 + (hash << 6) + (hash >> 2);
        }
        return hash;
    }
};

std::ostream& operator<<(std::ostream& os, const CubeState& state) {
    os << "CubeState:\n";
    for (int i = 0; i < 6; i++) {
        os << "Face " << i << ": ";
        for (int j = 0; j < 4; j++) {
            os << state.pieces[i * 4 + j] << " ";
        }
        os << "\n";
    }
    return os;
}

char getRandomMove() {
    vector<char> moves = {'F', 'U', 'R', 'B', 'D', 'L', 'f', 'u', 'r', 'b', 'd', 'l'};
    return moves[rand() % 6];
}

void rotateOneTime(vector<int>& pieces, const vector<int>& piecesToRotate) {
    int n = piecesToRotate.size();
    int last = pieces[piecesToRotate[n - 1]];
    for (int j = n - 1; j > 0; j--) {
        pieces[piecesToRotate[j]] = pieces[piecesToRotate[j - 1]];
    }
    pieces[piecesToRotate[0]] = last;
}

void fmove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {4, 5, 6, 7},
        {0, 17, 22, 11},
        {3, 16, 21, 10},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTime(state.pieces, arr);
    }
}

void umove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {0, 1, 2, 3},
        {5, 9, 13, 17},
        {6, 10, 14, 18},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTime(state.pieces, arr);
    }
}

void rmove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {16, 17, 18, 19},
        {6, 2, 12, 22},
        {7, 3, 13, 23},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTime(state.pieces, arr);
    }
}

void bmove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {12, 13, 14, 15},
        {2, 9, 20, 19},
        {1, 8, 23, 18}
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTime(state.pieces, arr);
    }
}

void dmove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {20, 21, 22, 23},
        {4, 16, 12, 8},
        {7, 19, 15, 11},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTime(state.pieces, arr);
    }
}

void lmove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {8, 9, 10, 11},
        {1, 5, 21, 15},
        {0, 4, 20, 14},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTime(state.pieces, arr);
    }
}

void rotateOneTimeOtherWay(vector<int>& pieces, const vector<int>& piecesToRotate) {
    int n = piecesToRotate.size();
    int first = pieces[piecesToRotate[0]];
    for (int j = 0; j < n - 1; j++) {
        pieces[piecesToRotate[j]] = pieces[piecesToRotate[j + 1]];
    }
    pieces[piecesToRotate[n - 1]] = first;
}

void fprimemove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {4, 5, 6, 7},
        {0, 17, 22, 11},
        {3, 16, 21, 10},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTimeOtherWay(state.pieces, arr);
    }
}

void uprimemove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {0, 1, 2, 3},
        {5, 9, 13, 17},
        {6, 10, 14, 18},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTimeOtherWay(state.pieces, arr);
    }
}

void rprimemove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {16, 17, 18, 19},
        {6, 2, 12, 22},
        {7, 3, 13, 23},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTimeOtherWay(state.pieces, arr);
    }
}

void bprimemove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {12, 13, 14, 15},
        {2, 9, 20, 19},
        {1, 8, 23, 18}
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTimeOtherWay(state.pieces, arr);
    }
}

void dprimemove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {20, 21, 22, 23},
        {4, 16, 12, 8},
        {7, 19, 15, 11},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTimeOtherWay(state.pieces, arr);
    }
}

void lprimemove(CubeState& state) {
    vector<vector<int>> piecesToRotate = {
        {8, 9, 10, 11},
        {1, 5, 21, 15},
        {0, 4, 20, 14},
    };
    for (auto& arr : piecesToRotate) {
        rotateOneTimeOtherWay(state.pieces, arr);
    }
}

char invertMove(char move) {
    if (islower(move))
        return toupper(move);
    else if (isupper(move))
        return tolower(move);
    else
        return move;
}

CubeState applyMoves(CubeState state, vector<char> moves) {
    for (char c : moves) {
        switch (c) {
            case 'F': fmove(state); break;
            case 'U': umove(state); break;
            case 'R': rmove(state); break;
            case 'B': bmove(state); break;
            case 'D': dmove(state); break;
            case 'L': lmove(state); break;
            case 'f': fprimemove(state); break;
            case 'u': uprimemove(state); break;
            case 'r': rprimemove(state); break;
            case 'b': bprimemove(state); break;
            case 'd': dprimemove(state); break;
            case 'l': lprimemove(state); break;
            default: break;
        }
    }
    return state;
}

vector<char> reconstructPath(CubeState intersection, 
                             unordered_map<CubeState, pair<CubeState, char>, CubeStateHash>& forwardParents,
                             unordered_map<CubeState, pair<CubeState, char>, CubeStateHash>& backwardParents) {
    vector<char> path;
    CubeState current = intersection;
    while (forwardParents.count(current)) {
        path.push_back(forwardParents[current].second);
        current = forwardParents[current].first;
    }

    for (char c : path) cout << c << ' ';
    cout << endl;

    reverse(path.begin(), path.end());

    current = intersection;
    vector<char> backwardPath;
    while (backwardParents.count(current)) {
        backwardPath.push_back(invertMove(backwardParents[current].second));
        current = backwardParents[current].first;
    }

    for (char c : path) cout << c << ' ';
    cout << endl;

    path.insert(path.end(), backwardPath.begin(), backwardPath.end());

    cout << "RECONSTRUCTED PATH\n";
    for (char c : path)
        cout << c << ' ';
    cout << '\n';
    return path;
}

CubeState bfs(queue<CubeState>& q,
              unordered_set<CubeState, CubeStateHash>& visited,
              unordered_map<CubeState, pair<CubeState, char>, CubeStateHash>& parents,
              unordered_set<CubeState, CubeStateHash>& otherVisited) {
    vector<char> moves = {'F', 'U', 'R', 'B', 'D', 'L', 'f', 'u', 'r', 'b', 'd', 'l'};
    int levelSize = q.size();
    for (int i = 0; i < levelSize; i++) {
        CubeState currentState = q.front();
        q.pop();
        for (char neighbor : moves) {
            CubeState newState = applyMoves(currentState, {neighbor});
            if (visited.find(newState) == visited.end()) {
                visited.insert(newState);
                parents[newState] = {currentState, neighbor};
                q.push(newState);
                if (otherVisited.find(newState) != otherVisited.end()) {
                    cout << "Intersection Found: " << newState << '\n';
                    return newState;
                }
            }
        }
    }
    return CubeState();
}

vector<char> bidirectionalBFS(CubeState start, CubeState end) {
    queue<CubeState> forwardq, backwardq;
    unordered_set<CubeState, CubeStateHash> forwardVisited, backwardVisited;
    unordered_map<CubeState, pair<CubeState, char>, CubeStateHash> forwardParents, backwardParents;
    forwardq.push(start);
    backwardq.push(end);
    forwardVisited.insert(start);
    backwardVisited.insert(end);

    while (!forwardq.empty() && !backwardq.empty()) {
        cout << "Forward Queue Size: " << forwardq.size() << '\n';
        cout << "Backward Queue Size: " << backwardq.size() << '\n';
        cout << "Forward Visited Size: " << forwardVisited.size() << '\n';
        cout << "Backward Visited Size: " << backwardVisited.size() << '\n';


        CubeState intersection = bfs(forwardq, forwardVisited, forwardParents, backwardVisited);
        if (intersection.valid) {
            return reconstructPath(intersection, forwardParents, backwardParents);
        }

        intersection = bfs(backwardq, backwardVisited, backwardParents, forwardVisited);
        if (intersection.valid) {
            return reconstructPath(intersection, forwardParents, backwardParents);
        }
        cout << "=======================================\n";
    }
    return {};
}

std::chrono::milliseconds randomScramble() {
    auto start = chrono::high_resolution_clock::now();
    CubeState cubeState;
    CubeState solvedState;
    cubeState.valid = true;
    solvedState.valid = true;

    srand(time(0));
    vector<char> scramble(20);
    for (char& c : scramble) {
        c = getRandomMove();
    }

    cout << "Scramble: ";
    for (char c : scramble) cout << c;
    cout << endl;

    cout << "solved state: " << endl << cubeState << endl;
    cubeState = applyMoves(cubeState, scramble);
    cout << "scramble applied: " << endl << cubeState << endl;

    vector<char> solution = bidirectionalBFS(cubeState, solvedState);
    cout << "solution: ";
    for (char c : solution) {
        cout << c;
    }
    cout << endl;

    cubeState = applyMoves(cubeState, solution);
    cout << "solution applied: " << endl;
    cout << cubeState;
    if (cubeState == solvedState) cout << "SOLVED" << endl;
    auto stop = chrono::high_resolution_clock::now();
    auto duration = chrono::duration_cast<chrono::milliseconds>(stop - start);
    return duration;
}

void runTrials() {
    int trials = 1;
    std::chrono::milliseconds total(0);
    for (int trial = 0; trial < trials; trial++) {
        auto duration = randomScramble();
        total += duration;
    }
    cout << (total / trials).count() << endl;
}

std::chrono::milliseconds manualInput() {
    auto start = chrono::high_resolution_clock::now();
    CubeState cubeState;
    CubeState solvedState;
    cubeState.valid = true;
    solvedState.valid = true;

    for (int i = 0; i < 24; i++) {
        cin >> cubeState.pieces[i];
    }
    cout << "solved state: " << endl << solvedState << endl;
    cout << "current state: " << endl << cubeState << endl;

    vector<char> solution = bidirectionalBFS(cubeState, solvedState);
    cout << "solution: ";
    for (char c : solution) {
        cout << c;
    }
    cout << endl;

    cubeState = applyMoves(cubeState, solution);
    cout << "solution applied: " << endl;
    cout << cubeState;
    if (cubeState == solvedState) cout << "SOLVED" << endl;
    auto stop = chrono::high_resolution_clock::now();
    auto duration = chrono::duration_cast<chrono::milliseconds>(stop - start);
    return duration;
}

int main() {
    // manualInput();
    runTrials();
}

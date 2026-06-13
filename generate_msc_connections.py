#!/usr/bin/env python3
"""
generate_msc_connections.py

Advanced MSC relation generator.
1. Computes TF-IDF from MSC prose.
2. Runs PageRank over citation networks.
3. Runs K-Means to cluster MSCs by feature similarity.
4. Generates an interactive visualizer (nodes sized by PageRank, colored by cluster).
"""

import csv
import math
import os
import random
import re
from collections import Counter, defaultdict

# --- CONFIGURATION ---
PROPOSALS_DIR = "proposals"
CSV_OUTPUT = "msc_connections.csv"
HTML_OUTPUT = "msc_connections.html"

# Weights for pairwise relationship score
W_DIRECT = 0.4
W_REF = 0.3
W_TEXT = 0.3

# Clustering configuration
NUM_CLUSTERS = 8
KMEANS_ITERATIONS = 15

# PageRank configuration
PAGERANK_DAMPING = 0.85
PAGERANK_ITERATIONS = 30


def parse_msc_metadata(filepath):
    filename = os.path.basename(filepath)
    msc_id_match = re.match(r"^(\d+)", filename)
    msc_id = msc_id_match.group(1) if msc_id_match else None

    title = ""
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#"):
                    title = re.sub(r"^#+\s*", "", line)
                    break
    if not title:
        title = filename.replace(".md", "")
    return msc_id, title


def extract_references(text, current_id):
    matches = re.findall(r"\bmsc\s*(\d+)\b", text, re.IGNORECASE)
    url_matches = re.findall(r"/pull/(\d+)", text)
    file_matches = re.findall(r"(\d+)-[^)]+\.md", text)
    all_refs = set(matches + url_matches + file_matches)
    return {ref for ref in all_refs if ref != current_id}


def tokenize(text):
    text = text.lower()
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)  # Strip code blocks
    text = re.sub(r"`[^`]+`", "", text)  # Strip inline code
    words = re.findall(r"\b[a-z]{3,}\b", text)

    stop_words = {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "you",
        "are",
        "not",
        "but",
        "all",
        "any",
        "can",
        "has",
        "have",
        "had",
        "was",
        "were",
        "been",
        "one",
        "two",
        "new",
        "out",
        "use",
        "using",
        "used",
        "user",
        "users",
        "will",
        "would",
        "should",
        "must",
        "their",
        "there",
        "they",
        "them",
        "our",
        "theirs",
        "under",
        "into",
        "over",
        "more",
        "some",
        "such",
        "than",
        "then",
        "only",
        "same",
        "other",
        "another",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "why",
        "whose",
        "whom",
        "about",
        "above",
        "after",
        "again",
        "against",
        "am",
        "an",
        "as",
        "at",
        "be",
        "because",
        "before",
        "being",
        "below",
        "between",
        "both",
        "by",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
    }
    return [w for w in words if w not in stop_words]


# --- TF-IDF VECTOR SPACE ---
def compute_tfidf(documents):
    N = len(documents)
    df = defaultdict(int)
    for tokens in documents.values():
        for token in set(tokens):
            df[token] += 1

    idf = {token: math.log((1 + N) / (1 + count)) + 1 for token, count in df.items()}
    vectors = {}
    for doc_id, tokens in documents.items():
        if not tokens:
            vectors[doc_id] = {}
            continue
        tf = Counter(tokens)
        vector = {
            token: (count / len(tokens)) * idf[token] for token, count in tf.items()
        }
        norm = math.sqrt(sum(val**2 for val in vector.values()))
        if norm > 0:
            vector = {token: val / norm for token, val in vector.items()}
        vectors[doc_id] = vector
    return vectors, idf


def cosine_similarity(vec1, vec2):
    if len(vec1) > len(vec2):
        vec1, vec2 = vec2, vec1
    return sum(val * vec2[token] for token, val in vec1.items() if token in vec2)


# --- PAGERANK CENTRALITY ---
def compute_pagerank(msc_citations, valid_ids):
    N = len(valid_ids)
    if N == 0:
        return {}

    # Initialize uniform PageRank
    pr = {node: 1.0 / N for node in valid_ids}

    # Track incoming linkages
    incoming = defaultdict(list)
    for node, refs in msc_citations.items():
        for ref in refs:
            incoming[ref].append(node)

    # Out-degree counts (dangling nodes redirect uniformly)
    out_counts = {node: len(refs) for node, refs in msc_citations.items()}

    for _ in range(PAGERANK_ITERATIONS):
        new_pr = {}
        # Sum PageRank from dangling nodes
        dangling_sum = sum(pr[node] for node in valid_ids if out_counts[node] == 0)

        for node in valid_ids:
            rank_sum = sum(pr[source] / out_counts[source] for source in incoming[node])
            # PageRank update formula
            new_pr[node] = ((1 - PAGERANK_DAMPING) / N) + PAGERANK_DAMPING * (
                rank_sum + (dangling_sum / N)
            )

        # Normalize to combat floating point precision drift
        total = sum(new_pr.values())
        pr = {node: val / total for node, val in new_pr.items()}

    return pr


# --- PURE PYTHON K-MEANS ---
def run_kmeans(tfidf_vectors, idf, k=NUM_CLUSTERS):
    doc_ids = list(tfidf_vectors.keys())
    if not doc_ids:
        return {}, {}

    # 1. Initialize centroids randomly from existing documents
    centroids = []
    initial_ids = random.sample(doc_ids, min(k, len(doc_ids)))
    for node_id in initial_ids:
        centroids.append(dict(tfidf_vectors[node_id]))

    assignments = {}

    for _ in range(KMEANS_ITERATIONS):
        # Assignment Step
        new_assignments = defaultdict(list)
        for doc_id in doc_ids:
            vector = tfidf_vectors[doc_id]
            best_sim = -1.0
            best_centroid = 0
            for c_idx, centroid in enumerate(centroids):
                sim = cosine_similarity(vector, centroid)
                if sim > best_sim:
                    best_sim = sim
                    best_centroid = c_idx
            new_assignments[best_centroid].append(doc_id)
            assignments[doc_id] = best_centroid

        # Update Step (recompute mean centroids)
        new_centroids = []
        for c_idx in range(k):
            assigned_docs = new_assignments[c_idx]
            if not assigned_docs:
                # If a centroid becomes empty, re-initialize randomly
                new_centroids.append(dict(tfidf_vectors[random.choice(doc_ids)]))
                continue

            sum_vector = defaultdict(float)
            for doc_id in assigned_docs:
                for token, val in tfidf_vectors[doc_id].items():
                    sum_vector[token] += val

            # Compute average
            mean_vector = {
                token: val / len(assigned_docs) for token, val in sum_vector.items()
            }
            # Re-normalize to unit length
            norm = math.sqrt(sum(val**2 for val in mean_vector.values()))
            if norm > 0:
                mean_vector = {token: val / norm for token, val in mean_vector.items()}
            new_centroids.append(mean_vector)

        centroids = new_centroids

    # Extract defining keywords for each cluster
    cluster_topics = {}
    for c_idx, centroid in enumerate(centroids):
        # Sort words in centroid by weight
        top_words = sorted(centroid.items(), key=lambda x: x[1], reverse=True)[:4]
        cluster_topics[c_idx] = ", ".join([word for word, val in top_words])

    return assignments, cluster_topics


def main():
    print("Parsing proposals...")
    msc_titles = {}
    msc_outgoing = {}
    msc_tokens = {}

    for file in os.listdir(PROPOSALS_DIR):
        if not file.endswith(".md"):
            continue
        filepath = os.path.join(PROPOSALS_DIR, file)
        msc_id, title = parse_msc_metadata(filepath)
        if not msc_id:
            continue

        msc_titles[msc_id] = title

        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        msc_outgoing[msc_id] = extract_references(content, msc_id)
        msc_tokens[msc_id] = tokenize(content)

    valid_ids = set(msc_titles.keys())
    msc_citations = {
        msc_id: refs.intersection(valid_ids) for msc_id, refs in msc_outgoing.items()
    }

    msc_incoming = defaultdict(set)
    for msc_id, refs in msc_citations.items():
        for ref in refs:
            msc_incoming[ref].add(msc_id)

    # 1. PageRank Calculation
    print("Computing PageRank Centrality...")
    pageranks = compute_pagerank(msc_citations, valid_ids)

    # 2. Vector space & K-Means clustering
    print("Analyzing text and performing K-Means clustering...")
    tfidf_vectors, idf = compute_tfidf(msc_tokens)
    cluster_assignments, cluster_topics = run_kmeans(tfidf_vectors, idf, NUM_CLUSTERS)

    # 3. Pairwise Relation Analysis
    print("Computing pairwise connection scores...")
    msc_list = sorted(list(valid_ids))
    num_mscs = len(msc_list)
    pairwise_connections = []

    for i in range(num_mscs):
        id_a = msc_list[i]
        for j in range(i + 1, num_mscs):
            id_b = msc_list[j]

            direct_link = (
                1.0
                if (id_b in msc_citations[id_a] or id_a in msc_citations[id_b])
                else 0.0
            )

            ref_a = msc_citations[id_a].union(msc_incoming[id_a])
            ref_b = msc_citations[id_b].union(msc_incoming[id_b])
            union_len = len(ref_a.union(ref_b))
            ref_overlap = (
                len(ref_a.intersection(ref_b)) / union_len if union_len > 0 else 0.0
            )

            text_sim = cosine_similarity(tfidf_vectors[id_a], tfidf_vectors[id_b])

            score = (
                (W_DIRECT * direct_link) + (W_REF * ref_overlap) + (W_TEXT * text_sim)
            )

            if score > 0.01:
                pairwise_connections.append(
                    {
                        "msc_a": id_a,
                        "title_a": msc_titles[id_a],
                        "msc_b": id_b,
                        "title_b": msc_titles[id_b],
                        "direct_link": round(direct_link, 3),
                        "reference_overlap": round(ref_overlap, 3),
                        "text_similarity": round(text_sim, 3),
                        "connection_score": round(score, 3),
                    }
                )

    # Sort connections by score
    pairwise_connections.sort(key=lambda x: x["connection_score"], reverse=True)

    # Write CSV
    print(f"Writing CSV output to {CSV_OUTPUT}...")
    with open(CSV_OUTPUT, "w", newline="", encoding="utf-8") as f:
        # Save node stats as discrete metadata, and output the relationships
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "msc_a",
                "title_a",
                "msc_b",
                "title_b",
                "direct_link",
                "reference_overlap",
                "text_similarity",
                "connection_score",
            ],
        )
        writer.writeheader()
        writer.writerows(pairwise_connections)

    # Prepare node properties for HTML
    # We scale PageRanks to standard visual ranges
    max_pr = max(pageranks.values()) if pageranks else 1.0

    html_nodes = []
    for node_id in valid_ids:
        html_nodes.append(
            {
                "id": node_id,
                "label": f"MSC{node_id}: {msc_titles[node_id]}",
                "pagerank": pageranks[node_id],
                "norm_pr": pageranks[node_id] / max_pr,
                "cluster": cluster_assignments[node_id],
                "topic": cluster_topics[cluster_assignments[node_id]],
            }
        )

    # Keep visualization links strong
    vis_links = [c for c in pairwise_connections if c["connection_score"] >= 0.12]
    html_links = [
        {
            "source": lnk["msc_a"],
            "target": lnk["msc_b"],
            "value": lnk["connection_score"],
        }
        for lnk in vis_links
    ]

    # Write HTML
    print(f"Writing browser-visualizer to {HTML_OUTPUT}...")

    # Generate distinct colors for clusters
    colors = [
        "#ff595e",
        "#ffca3a",
        "#8ac926",
        "#1982c4",
        "#6a4c93",
        "#e36414",
        "#00b4d8",
        "#f15bb5",
    ]

    legend_items = []
    for i, topic in cluster_topics.items():
        col = colors[i % len(colors)]
        legend_items.append(
            f'<div class="legend-item">'
            f'<div class="color-box" style="background-color: {col}"></div>'
            f"<span><strong>Group {i}</strong>: {topic}</span>"
            f"</div>"
        )
    legend_html = "".join(legend_items)

    import json

    html_template = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>MSC Centrality & Feature Clusters</title>
    <script src="https://d3js.org/d3.v7.min.js"></script>
    <style>
        body {
            margin: 0;
            font-family: -apple-system, BlinkMacSystemFont,
                "Segoe UI", Roboto, sans-serif;
            background-color: #111;
            color: #eee;
            overflow: hidden;
        }
        #header {
            position: absolute;
            top: 15px;
            left: 15px;
            pointer-events: none;
        }
        h1 {
            margin: 0 0 5px 0;
            font-size: 22px;
            color: #fff;
        }
        p {
            margin: 0 0 10px 0;
            font-size: 13px;
            color: #aaa;
        }
        #legend {
            font-size: 11px;
            background: rgba(30,30,30,0.85);
            padding: 10px;
            border-radius: 6px;
            border: 1px solid #333;
            pointer-events: auto;
            max-width: 320px;
        }
        .legend-item {
            display: flex;
            align-items: center;
            margin-bottom: 4px;
        }
        .color-box {
            width: 12px;
            height: 12px;
            border-radius: 3px;
            margin-right: 8px;
        }
        .node {
            cursor: pointer;
            stroke: #111;
            stroke-width: 1.5px;
            transition: stroke 0.15s;
        }
        .node:hover {
            stroke: #fff;
            stroke-width: 2.5px;
        }
        .link {
            stroke-opacity: 0.5;
            stroke: #444;
        }
        .label {
            font-size: 10px;
            fill: #aaa;
            pointer-events: none;
            font-weight: 500;
        }
        #tooltip {
            position: absolute;
            display: none;
            background: rgba(15,15,15,0.95);
            padding: 10px 14px;
            border-radius: 6px;
            border: 1px solid #444;
            font-size: 12px;
            pointer-events: none;
            max-width: 350px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5);
            line-height: 1.4;
        }
    </style>
</head>
<body>
    <div id="header">
        <h1>MSC Centrality & Feature Clusters</h1>
        <p>Nodes sized by <strong>PageRank</strong> &bull; Colored by cluster</p>
        <div id="legend">
            <strong style="font-size: 12px; display: block; margin-bottom: 6px;">
                Thematic Feature Clusters:
            </strong>
            __LEGEND_HTML__
        </div>
    </div>
    <div id="tooltip"></div>
    <svg id="network"></svg>

    <script>
        const width = window.innerWidth;
        const height = window.innerHeight;
        const colors = __COLORS__;

        const svg = d3.select("#network")
            .attr("width", width)
            .attr("height", height);

        const g = svg.append("g");

        svg.call(d3.zoom().scaleExtent([0.1, 4]).on("zoom", (event) => {
            g.attr("transform", event.transform);
            const k = event.transform.k;
            label.text(d => {
                if (k >= 1.2) {
                    return d.label;
                } else if (k >= 0.6) {
                    return "MSC" + d.id;
                } else {
                    return "";
                }
            })
            .style("fill-opacity", k >= 0.6 ? 1.0 : 0.0);
        }));

        const nodes = __NODES__;
        const links = __LINKS__;

        const simulation = d3.forceSimulation(nodes)
            .force("link", d3.forceLink(links).id(d => d.id).distance(90))
            .force("charge", d3.forceManyBody().strength(-150))
            .force("center", d3.forceCenter(width / 2, height / 2))
            .force("collision", d3.forceCollide().radius(
                d => Math.max(5, d.norm_pr * 22) + 4
            ));

        const link = g.append("g")
            .selectAll("line")
            .data(links)
            .join("line")
            .attr("class", "link")
            .attr("stroke-width", d => Math.max(1, d.value * 4.5));

        const node = g.append("g")
            .selectAll("circle")
            .data(nodes)
            .join("circle")
            .attr("class", "node")
            .attr("r", d => Math.max(5, d.norm_pr * 22))
            .attr("fill", d => colors[d.cluster % colors.length])
            .call(drag(simulation));

        const label = g.append("g")
            .selectAll("text")
            .data(nodes)
            .join("text")
            .attr("class", "label")
            .text(d => "MSC" + d.id);

        const tooltip = d3.select("#tooltip");

        node.on("mouseover", (event, d) => {
            tooltip.style("display", "block").html(
                '<div style="font-size:13px; font-weight:bold; ' +
                'margin-bottom:5px; color:#fff;">' + d.label + '</div>' +
                '<div style="margin-bottom:3px;">' +
                '<strong>PageRank Authority:</strong> ' +
                (d.pagerank * 100).toFixed(3) + '%</div>' +
                '<div><strong>Thematic Cluster:</strong> Group ' +
                d.cluster + ' (' + d.topic + ')</div>'
            );
        })
        .on("mousemove", (event) => {
            tooltip.style("left", (event.pageX + 12) + "px")
                   .style("top", (event.pageY - 20) + "px");
        })
        .on("mouseout", () => {
            tooltip.style("display", "none");
        });

        simulation.on("tick", () => {
            link
                .attr("x1", d => d.source.x)
                .attr("y1", d => d.source.y)
                .attr("x2", d => d.target.x)
                .attr("y2", d => d.target.y);

            node
                .attr("cx", d => d.x)
                .attr("cy", d => d.y);

            label
                .attr("x", d => d.x + Math.max(6, d.norm_pr * 22) + 3)
                .attr("y", d => d.y + 3);
        });

        function drag(simulation) {
            return d3.drag()
                .on("start", (event, d) => {
                    if (!event.active) simulation.alphaTarget(0.3).restart();
                    d.fx = d.x;
                    d.fy = d.y;
                })
                .on("drag", (event, d) => {
                    d.fx = event.x;
                    d.fy = event.y;
                })
                .on("end", (event, d) => {
                    if (!event.active) simulation.alphaTarget(0);
                    d.fx = null;
                    d.fy = null;
                });
        }
    </script>
</body>
</html>
"""

    html_content = (
        html_template.replace("__LEGEND_HTML__", legend_html)
        .replace("__COLORS__", json.dumps(colors))
        .replace("__NODES__", json.dumps(html_nodes))
        .replace("__LINKS__", json.dumps(html_links))
    )

    with open(HTML_OUTPUT, "w", encoding="utf-8") as f:
        f.write(html_content)

    print("Success! Open 'msc_connections.html' in any browser to visualize.")


if __name__ == "__main__":
    main()

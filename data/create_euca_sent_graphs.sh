#!/bin/bash

for dataset in eu ca; do
    for setup in head_final head_first; do

        # basic version
        python3 convert_kafs.py --json_dir $dataset --out_dir sent_graphs/$dataset --setup $setup
        # adding inside_labels
        python3 convert_kafs.py --json_dir $dataset --out_dir sent_graphs/$dataset --setup $setup --inside_label
        # adding inside_labels and using dependency edges to redefine heads
        python3 convert_kafs.py --json_dir $dataset --out_dir sent_graphs/$dataset --setup $setup --inside_label --use_dep_edges
        # adding inside_labels and using dependency edges and dependency labels
        # to redefine heads
        python3 convert_kafs.py --json_dir $dataset --out_dir sent_graphs/$dataset --setup $setup --inside_label --use_dep_edges --use_dep_labels
    done;

    # a baseline where the first token of the sentiment expression is set as
    # the root and every dependency relation points to this token
    python3 convert_kafs.py --json_dir $dataset --out_dir sent_graphs/$dataset --setup point_to_root
done;

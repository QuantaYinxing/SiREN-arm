// Single-tool bbox crop of a Geofabrik .osm.pbf using libosmium (headers are
// vendored in .sandbox/include from the pyosmium 4.3.2 sdist; zlib from the
// system). Same semantics as the old pyosmium crop: keep ways that touch the
// bbox (any node inside), relations whose member ways/refs are kept, and all
// nodes referenced by kept objects plus nodes inside the bbox - so the output
// is self-consistent for Valhalla and protomaps. Four streaming C++ passes,
// far faster than Python callbacks over a 1.7 GB extract.
//
// Compile (once):
//   g++ -O2 -std=c++20 -DHAVE_PROTOZERO -DHAVE_ZLIB \
//     -I .sandbox/include/libosmium -I .sandbox/include/protozero \
//     scripts/osm_crop.cpp -o .sandbox/bin/osm_bbox -lz
//
// Usage: .sandbox/bin/osm_bbox in.pbf out.pbf lo_lat hi_lat lo_lon hi_lon

#include <cstdint>
#include <cstdio>
#include <string>
#include <unordered_set>

#include <osmium/io/pbf_input.hpp>
#include <osmium/io/pbf_output.hpp>
#include <osmium/io/reader.hpp>
#include <osmium/io/reader_iterator.hpp>
#include <osmium/io/writer.hpp>
#include <osmium/osm/item_type.hpp>
#include <osmium/osm/node.hpp>
#include <osmium/osm/relation.hpp>
#include <osmium/osm/way.hpp>

int main(int argc, char** argv) {
    if (argc != 7) {
        std::fprintf(stderr, "usage: osm_bbox in.pbf out.pbf lo_lat hi_lat lo_lon hi_lon\n");
        return 2;
    }
    const std::string src = argv[1];
    const std::string dst = argv[2];
    const double lo_lat = std::stod(argv[3]);
    const double hi_lat = std::stod(argv[4]);
    const double lo_lon = std::stod(argv[5]);
    const double hi_lon = std::stod(argv[6]);

    std::unordered_set<uint64_t> inside;
    {
        osmium::io::Reader reader{src, osmium::osm_entity_bits::node};
        for (const auto& entity : reader) {
            if (entity.type() != osmium::item_type::node) continue;
            const auto& n = static_cast<const osmium::Node&>(entity);
            if (n.location().lat() >= lo_lat && n.location().lat() <= hi_lat
                && n.location().lon() >= lo_lon && n.location().lon() <= hi_lon) {
                inside.insert(n.id());
            }
        }
        reader.close();
    }

    std::unordered_set<uint64_t> keep_ways;
    std::unordered_set<uint64_t> way_refs;
    {
        osmium::io::Reader reader{src, osmium::osm_entity_bits::way};
        for (const auto& entity : reader) {
            const auto& w = static_cast<const osmium::Way&>(entity);
            bool hit = false;
            for (const auto& nr : w.nodes()) {
                if (inside.count(nr.ref()) != 0) { hit = true; break; }
            }
            if (hit) {
                keep_ways.insert(w.id());
                for (const auto& nr : w.nodes()) way_refs.insert(nr.ref());
            }
        }
        reader.close();
    }

    std::unordered_set<uint64_t> keep_rels;
    std::unordered_set<uint64_t> rel_node_refs;
    {
        osmium::io::Reader reader{src, osmium::osm_entity_bits::relation};
        for (const auto& entity : reader) {
            const auto& r = static_cast<const osmium::Relation&>(entity);
            bool hit = false;
            for (const auto& m : r.members()) {
                if (m.type() == osmium::item_type::way && keep_ways.count(m.ref()) != 0) hit = true;
                if (m.type() == osmium::item_type::node) rel_node_refs.insert(m.ref());
            }
            if (hit) keep_rels.insert(r.id());
        }
        reader.close();
    }

    std::unordered_set<uint64_t> need_nodes = inside;
    need_nodes.insert(way_refs.begin(), way_refs.end());
    need_nodes.insert(rel_node_refs.begin(), rel_node_refs.end());

    osmium::io::Writer writer{osmium::io::File{dst}}; // format/compression from the .osm.pbf filename
    {
        osmium::io::Reader reader{src, osmium::osm_entity_bits::relation};
        for (const auto& entity : reader) {
            if (keep_rels.count(static_cast<const osmium::OSMObject&>(entity).id()) != 0) writer(entity);
        }
        reader.close();
    }
    {
        osmium::io::Reader reader{src, osmium::osm_entity_bits::way};
        for (const auto& entity : reader) {
            if (keep_ways.count(static_cast<const osmium::OSMObject&>(entity).id()) != 0) writer(entity);
        }
        reader.close();
    }
    {
        osmium::io::Reader reader{src, osmium::osm_entity_bits::node};
        for (const auto& entity : reader) {
            if (need_nodes.count(static_cast<const osmium::OSMObject&>(entity).id()) != 0) writer(entity);
        }
        reader.close();
    }
    writer.close();

    std::printf("crop: %zu bbox nodes, %zu ways, %zu relations; %zu nodes written -> %s\n",
                inside.size(), keep_ways.size(), keep_rels.size(), need_nodes.size(), dst.c_str());
    return 0;
}

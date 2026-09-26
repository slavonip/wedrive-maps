// WeDrive: the FRONTIER of one country graph — every graph node where a drivable edge leaves the
// country. It is the per-country half of what portal_border produced for a PAIR of graphs, and it
// is what makes country versions independent: each package carries its own frontier, and the
// device joins two frontiers at runtime (scripts/regional-frontier.py, Frontier.kt — rule D1).
//
// THE CRITERION IS portal_border's, deliberately unchanged. A country graph is built with an admin
// database that knows only that country, so a node beyond the border has no country at all; an
// auto-accessible, non-shortcut edge whose two ends disagree about the country crosses the border.
// Both ends of such an edge are frontier nodes. Same criterion -> the same pairs the proven pair
// tables held (measured 2026-09-26: 1812 of 1812 in both versions).
//
// Reads tiles only; no region is involved, so the ids printed are the graph's own (region bits 0).
//
//   frontier_dump <tile_dir>
//   out (one line per node, space separated, '-' for an empty field):
//     level lat_e7 lon_e7 local_id here there crossing_way_ids all_way_ids tile_osm_ids
//
// tile_osm_ids is filled only when the graph was built with keep_osm_node_ids; production builds
// do not do that (the OSM id comes from the source PBF, scripts/regional-frontier.py).
#include <cmath>
#include <cstdio>
#include <set>
#include <string>

#include <boost/property_tree/ptree.hpp>
#include <valhalla/baldr/graphconstants.h>
#include <valhalla/baldr/graphreader.h>
#include <valhalla/baldr/graphtile.h>
#include <valhalla/baldr/tilehierarchy.h>

using namespace valhalla::baldr;
using namespace valhalla::midgard;

namespace {

std::string Country(const graph_tile_ptr& t, const NodeInfo* n) {
  const uint32_t i = n->admin_index();
  if (i >= t->header()->admincount()) {
    return std::string();
  }
  return t->admininfo(i).country_iso();
}

std::string Join(const std::set<uint64_t>& s) {
  std::string o;
  for (auto v : s) {
    o += (o.empty() ? "" : ",") + std::to_string(v);
  }
  return o.empty() ? "-" : o;
}

std::string OrDash(const std::string& s) {
  return s.empty() ? "-" : s;
}

} // namespace

int main(int argc, char** argv) {
  if (argc != 2) {
    std::fprintf(stderr, "usage: %s <tile_dir>\n", argv[0]);
    return 2;
  }
  boost::property_tree::ptree conf;
  conf.put("tile_dir", argv[1]);
  conf.put("max_cache_size", 3ull * 1024 * 1024 * 1024);
  GraphReader reader(conf);

  size_t nodes = 0, frontier = 0;
  for (uint8_t level = 0; level <= 2; ++level) {
    for (const GraphId& base : reader.GetTileSet(level)) {
      auto tile = reader.GetGraphTile(base);
      if (!tile) {
        continue;
      }
      const auto tbase = tile->header()->base_ll();
      for (uint32_t i = 0; i < tile->header()->nodecount(); ++i) {
        ++nodes;
        const auto* n = tile->node(i);
        const std::string here = Country(tile, n);
        std::set<uint64_t> cross, all, osm;
        std::string there;
        bool border = false;
        for (uint32_t e = 0; e < n->edge_count(); ++e) {
          const auto* de = tile->directededge(n->edge_index() + e);
          if (de->is_shortcut()) {
            continue;
          }
          const auto info = tile->edgeinfo(de);
          if (info.wayid()) {
            all.insert(info.wayid());
          }
          const auto ids = info.osm_node_ids();
          if (!ids.empty()) {
            osm.insert(de->forward() ? ids.front() : ids.back());
          }
          if (!(de->forwardaccess() & kAutoAccess)) {
            continue;
          }
          auto etile = reader.GetGraphTile(de->endnode());
          if (!etile || de->endnode().id() >= etile->header()->nodecount()) {
            continue;
          }
          const std::string t = Country(etile, etile->node(de->endnode().id()));
          if (t != here) {
            border = true;
            cross.insert(info.wayid());
            if (there.empty()) {
              there = t;
            }
          }
        }
        if (!border) {
          continue;
        }
        ++frontier;
        const PointLL ll = n->latlng(tbase);
        std::printf("%d %lld %lld %llu %s %s %s %s %s\n", level, std::llround(ll.lat() * 1e7),
                    std::llround(ll.lng() * 1e7),
                    static_cast<unsigned long long>(GraphId(base.tileid(), level, i).value),
                    OrDash(here).c_str(), OrDash(there).c_str(), Join(cross).c_str(),
                    Join(all).c_str(), Join(osm).c_str());
      }
    }
  }
  std::fprintf(stderr, "# frontier_dump: nodes %zu, frontier %zu\n", nodes, frontier);
  return 0;
}

// Copyright 2020 the V8 project authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef V8_FUZZILLI_COV_H_
#define V8_FUZZILLI_COV_H_

// This file is defining functions to handle coverage which are needed for
// fuzzilli fuzzer It communicates coverage bitmap with fuzzilli through shared
// memory
// https://clang.llvm.org/docs/SanitizerCoverage.html

#include <cstdint>
#include <vector>

// Shared memory layout:
//   [0x000000] uint32_t num_edges
//   [0x000004] uint8_t edges[]          (bit-packed code coverage)
//              ...
//   [TYPE_COV_OFFSET] uint8_t type_bits[] (bit-packed type coverage)
//   [SHM_SIZE]  end
#define SHM_SIZE           0x200000
#define TYPE_COV_MAX_LOCATIONS 1024
#define TYPE_COV_MAX_TYPES     4096
#define TYPE_COV_BITMAP_SIZE   ((TYPE_COV_MAX_LOCATIONS * TYPE_COV_MAX_TYPES) / 8)
#define TYPE_COV_OFFSET        (SHM_SIZE - TYPE_COV_BITMAP_SIZE)

void fuzzilli_cov_enable();
void sanitizer_cov_reset_edgeguards();
uint32_t sanitizer_cov_count_discovered_edges();
void cov_init_builtins_edges(uint32_t num_edges);
void cov_update_builtins_basic_block_coverage(const std::vector<bool>& cov_map);

void record_type(uint16_t location_id, uint16_t type_id);

namespace v8::internal::compiler {
class JSHeapBroker;
inline JSHeapBroker* as_mutable_broker(JSHeapBroker* b) { return b; }
inline JSHeapBroker* as_mutable_broker(const JSHeapBroker* b) {
  return const_cast<JSHeapBroker*>(b);
}
}  // namespace v8::internal::compiler

// Instrumentation macros for type coverage.
// MapRef has a direct instance_type() accessor.
// HeapObjectRef/JSObjectRef need a broker to obtain the map first.

#define RECORD_MAPREF(mapref, location) \
  record_type((location), static_cast<uint16_t>((mapref).instance_type()))

#define RECORD_OPTIONAL_MAPREF(mapref, location)                          \
  do {                                                                    \
    if ((mapref).has_value())                                             \
      record_type((location),                                             \
                  static_cast<uint16_t>((mapref)->instance_type()));       \
  } while (0)

#define RECORD_HEAPOBJECTREF(heapobjectref, broker, location)             \
  record_type((location), static_cast<uint16_t>(                          \
      (heapobjectref).GetHeapObjectType(                                  \
          as_mutable_broker(broker)).instance_type()))

#define RECORD_OPTIONAL_HEAPOBJECTREF(heapobjectref, broker, location)    \
  do {                                                                    \
    if ((heapobjectref).has_value())                                      \
      record_type((location), static_cast<uint16_t>(                      \
          (heapobjectref)->GetHeapObjectType(                             \
              as_mutable_broker(broker)).instance_type()));                \
  } while (0)

#define RECORD_JSOBJECTREF(jsobjectref, broker, location) \
  RECORD_HEAPOBJECTREF(jsobjectref, broker, location)

#define RECORD_OPTIONAL_JSOBJECTREF(jsobjectref, broker, location) \
  RECORD_OPTIONAL_HEAPOBJECTREF(jsobjectref, broker, location)

#define RECORD_JSRECEIVERREF(ref, broker, location) \
  RECORD_HEAPOBJECTREF(ref, broker, location)

#define RECORD_OPTIONAL_JSRECEIVERREF(ref, broker, location) \
  RECORD_OPTIONAL_HEAPOBJECTREF(ref, broker, location)

#define RECORD_NAMEREF(ref, broker, location) \
  RECORD_HEAPOBJECTREF(ref, broker, location)

#define RECORD_OPTIONAL_NAMEREF(ref, broker, location) \
  RECORD_OPTIONAL_HEAPOBJECTREF(ref, broker, location)

#define RECORD_STRINGREF(ref, broker, location) \
  RECORD_HEAPOBJECTREF(ref, broker, location)

#define RECORD_OPTIONAL_STRINGREF(ref, broker, location) \
  RECORD_OPTIONAL_HEAPOBJECTREF(ref, broker, location)

#define RECORD_JSFUNCTIONREF(ref, broker, location) \
  RECORD_HEAPOBJECTREF(ref, broker, location)

#define RECORD_OPTIONAL_JSFUNCTIONREF(ref, broker, location) \
  RECORD_OPTIONAL_HEAPOBJECTREF(ref, broker, location)

#define RECORD_FIXEDARRAYBASEREF(ref, broker, location) \
  RECORD_HEAPOBJECTREF(ref, broker, location)

#define RECORD_OPTIONAL_FIXEDARRAYBASEREF(ref, broker, location) \
  RECORD_OPTIONAL_HEAPOBJECTREF(ref, broker, location)

#define RECORD_FIXEDARRAYREF(ref, broker, location) \
  RECORD_HEAPOBJECTREF(ref, broker, location)

#define RECORD_OPTIONAL_FIXEDARRAYREF(ref, broker, location) \
  RECORD_OPTIONAL_HEAPOBJECTREF(ref, broker, location)

#define RECORD_CONTEXTREF(ref, broker, location) \
  RECORD_HEAPOBJECTREF(ref, broker, location)

#define RECORD_OPTIONAL_CONTEXTREF(ref, broker, location) \
  RECORD_OPTIONAL_HEAPOBJECTREF(ref, broker, location)

#endif  // V8_FUZZILLI_COV_H_

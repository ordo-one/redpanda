/*
 * Copyright 2023 Redpanda Data, Inc.
 *
 * Use of this software is governed by the Business Source License
 * included in the file licenses/BSL.md
 *
 * As of the Change Date specified in that file, in accordance with
 * the Business Source License, use of this software will be governed
 * by the Apache License, Version 2.0
 */
#pragma once

#include "cluster/errc.h"
#include "cluster/fwd.h"
#include "features/fwd.h"
#include "model/metadata.h"
#include "model/transform.h"
#include "raft/fwd.h"
#include "seastarx.h"
#include "transform/fwd.h"
#include "wasm/fwd.h"

#include <seastar/core/sharded.hh>

namespace transform {

/**
 * The transform service is responsible for intersecting the current state of
 * plugins and topic partitions and ensures that the corresponding wasm
 * transform is running for each leader partition (on the input topic).
 *
 * Instances on every shard.
 */
class service : public ss::peering_sharded_service<service> {
public:
    service(
      wasm::runtime* runtime,
      model::node_id self,
      ss::sharded<cluster::plugin_frontend>* plugin_frontend,
      ss::sharded<features::feature_table>* feature_table,
      ss::sharded<raft::group_manager>* group_manager,
      ss::sharded<cluster::partition_manager>* partition_manager,
      ss::sharded<rpc::client>* rpc_client);
    service(const service&) = delete;
    service(service&&) = delete;
    service& operator=(const service&) = delete;
    service& operator=(service&&) = delete;
    ~service();

    ss::future<> start();
    ss::future<> stop();

    /**
     * Deploy a transform to the cluster.
     */
    ss::future<cluster::errc>
      deploy_transform(model::transform_metadata, iobuf);

    /**
     * Delete a transform from the cluster.
     */
    ss::future<cluster::errc> delete_transform(model::transform_name);

private:
    ss::gate _gate;

    wasm::runtime* _runtime;
    model::node_id _self;
    ss::sharded<cluster::plugin_frontend>* _plugin_frontend;
    ss::sharded<features::feature_table>* _feature_table;
    ss::sharded<raft::group_manager>* _group_manager;
    ss::sharded<cluster::partition_manager>* _partition_manager;
    ss::sharded<rpc::client>* _rpc_client;
};

} // namespace transform

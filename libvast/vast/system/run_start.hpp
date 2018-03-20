/******************************************************************************
 *                    _   _____   __________                                  *
 *                   | | / / _ | / __/_  __/     Visibility                   *
 *                   | |/ / __ |_\ \  / /          Across                     *
 *                   |___/_/ |_/___/ /_/       Space and Time                 *
 *                                                                            *
 * This file is part of VAST. It is subject to the license terms in the       *
 * LICENSE file found in the top-level directory of this distribution and at  *
 * http://vast.io/license. No part of VAST, including this file, may be       *
 * copied, modified, propagated, or distributed except according to the terms *
 * contained in the LICENSE file.                                             *
 ******************************************************************************/

#ifndef VAST_SYSTEM_RUN_START_HPP
#define VAST_SYSTEM_RUN_START_HPP

#include <memory>
#include <string>
#include <string_view>

#include <caf/detail/unordered_flat_map.hpp>

#include "vast/system/base_command.hpp"

namespace vast::system {

/// Default implementation for the `start` command.
/// @relates application
class run_start : public base_command {
public:
  run_start(command* parent, std::string_view name);

protected:
  int run_impl(caf::actor_system& sys, opt_map& options,
               caf::message args) override;

private:
  /// Spawn empty node without components if set.
  bool spawn_bare_node;

  /// Run VAST in foreground (do not daemonize) if set.
  bool in_foreground;
};

} // namespace vast::system

#endif

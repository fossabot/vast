//    _   _____   __________
//   | | / / _ | / __/_  __/     Visibility
//   | |/ / __ |_\ \  / /          Across
//   |___/_/ |_/___/ /_/       Space and Time
//
// SPDX-FileCopyrightText: (c) 2021 The VAST Contributors
// SPDX-License-Identifier: BSD-3-Clause

#define SUITE rest_authentication

#include "web/authenticator.hpp"

#include <vast/test/test.hpp>

TEST(token validation) {
  vast::plugins::web::authenticator_state state;
  auto token = state.generate();
  REQUIRE_NOERROR(token);
  CHECK_EQUAL(state.authenticate(*token), true);
  CHECK_EQUAL(state.authenticate("Shub-Niggurath"), false);
  auto serialized_state = state.save();
  REQUIRE_NOERROR(serialized_state);
  vast::plugins::web::authenticator_state recovered_state;
  recovered_state.initialize_from(*serialized_state);
  CHECK_EQUAL(state.authenticate(*token), true);
  CHECK_EQUAL(state.authenticate("Yog-Sothoth"), false);
}

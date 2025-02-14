//    _   _____   __________
//   | | / / _ | / __/_  __/     Visibility
//   | |/ / __ |_\ \  / /          Across
//   |___/_/ |_/___/ /_/       Space and Time
//
// SPDX-FileCopyrightText: (c) 2022 The VAST Contributors
// SPDX-License-Identifier: BSD-3-Clause

#include "web/restinio_response.hpp"

namespace vast::plugins::web {

static std::string content_type_to_string(vast::http_content_type type) {
  switch (type) {
    case http_content_type::json:
      return "application/json; charset=utf-8";
    case http_content_type::ldjson:
      return "application/ld+json; charset=utf-8";
  }
  // Unreachable
  return "application/octet-stream";
}

restinio_response::restinio_response(request_handle_t&& handle,
                                     const rest_endpoint& endpoint)
  : request_(std::move(handle)),
    // Note that ownership of the `connection` is transferred when creating a
    // response.
    response_(request_->create_response<restinio::user_controlled_output_t>()) {
  response_.append_header(restinio::http_field::content_type,
                          content_type_to_string(endpoint.content_type));
}

restinio_response::~restinio_response() {
  // `done()` must only be called exactly once.
  response_.append_header_date_field().set_content_length(body_size_).done();
}

void restinio_response::append(std::string body) {
  body_size_ += body.size();
  response_.append_body(std::move(body));
}

void restinio_response::abort(uint16_t error_code, std::string message) {
  response_.header().status_code(restinio::http_status_code_t{error_code});
  body_size_ = message.size();
  response_.set_body(std::move(message));
  // TODO: Proactively call `done()` here, and add some flag to prevent
  // it from being called multiple times.
}

auto restinio_response::request() const -> const request_handle_t& {
  return request_;
}

} // namespace vast::plugins::web

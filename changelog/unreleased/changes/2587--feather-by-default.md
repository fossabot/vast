The default store-backend of VAST is now `feather`. Reading from VAST's custom
`segment-store` backend is still transparently supported, but new partitions
automatically write to the Apache Feather V2 backend instead.

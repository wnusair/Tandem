//! Language backends that turn source into a WASM artifact. Adding a language
//! means adding a module here that implements
//! [`CompileBackend`](crate::compile::CompileBackend).

pub mod componentize_py;

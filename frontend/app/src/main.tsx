import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./auth";
import "./index.css";

// AuthProvider sits inside BrowserRouter (Login uses no routing of its own,
// but the public /track/:ref route below it does) and outside App, so every
// screen can call useAuth() and api.ts has a token before the first fetch.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);

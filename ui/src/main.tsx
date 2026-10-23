import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

if (location.hash.startsWith("#/overlay") || location.hash.startsWith("#/pane")) document.documentElement.classList.add("overlay");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

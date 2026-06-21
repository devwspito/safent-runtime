"""Roster multi-agente del SO — entidades 'Agent' propiedad del daemon.

El SO admite varios agentes; trae uno 'default' y el operador crea/edita/elimina
otros. Cada agente posee su persona, instrucciones y (por agent_id) sus
conversaciones, skills y tareas. Es estado NATIVO del daemon (Principio 0): se
gestiona por el control-plane D-Bus, jamás por CRUD HTTP.
"""

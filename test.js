const q = `
          [out:json];
          (
            way(around:35,41.8781,-87.6298)["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|service)$"];
            way(around:35,41.8781,-87.6298)["railway"];
          );
          out geom;
        `;
console.log(encodeURIComponent(q));
